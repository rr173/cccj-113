"""单表资料的读取与校验。

资料由两份文件构成：

- **列清单**（JSON）：``{"table_id": "...", "columns": ["supplier", "part", ...]}``
- **逐行文件**（JSON Lines）：每行一条记录
  ``{"row_id": "R000001", "values": {"supplier": "S01", "part": "P-1", ...}}``

值的域只有**字符串或 null**：``""`` 是实际值；前导零、大小写与空格都按字面
保留，绝不 strip / lower / 数值化。列名同样按字面匹配。

校验规则（全部问题一次性收集，定位到文件行号与列名后整体拒绝）：

- 列清单缺 ``columns`` / 重复列名 / 列名不是非空字符串；
- 行不是 JSON 对象、缺少 ``row_id``、``row_id`` 重复或为空；
- 行内缺列（少列）、出现列清单之外的列（多列）、同一 JSON 对象内重复键；
- 值不是字符串也不是 null（数字、布尔、数组、对象一律拒绝）。

文件行号从 1 开始（含表头/空行计数），便于直接定位；空白行会被跳过。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .models import CellValue, Dataset, TableRow

_ROW_KEYS = frozenset({"row_id", "values"})
_COLUMN_KEYS = frozenset({"table_id", "columns"})


class DepsInputError(ValueError):
    """资料不合法：携带全部问题描述（含行号/列名），拒绝处理。"""

    status = "INVALID_INPUT"

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("资料不合法，拒绝处理:\n" + "\n".join(f"- {p}" for p in self.problems))


class DepsParameterError(ValueError):
    """k / min_rows / 规则参数不合法：拒绝处理。"""

    status = "INVALID_INPUT"

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("参数不合法，拒绝处理:\n" + "\n".join(f"- {p}" for p in self.problems))


class ScaleExceeded(ValueError):
    """资料规模超出已声明范围：提前拒绝，不产出任何「完整」发现。"""

    status = "SCALE_EXCEEDED"

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("超出已声明的规模范围，拒绝处理:\n"
                         + "\n".join(f"- {p}" for p in self.problems))


def digest_files(paths: Sequence[str | Path]) -> str:
    """输入文件字节级 SHA-256（按文件名排序拼接），用于结果可追溯。"""
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _no_duplicate_keys(pairs):
    """JSON object_pairs_hook：任何层级出现重复键都报错（不静默取最后一个）。"""
    seen = set()
    out = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"同一 JSON 对象内重复键 {key!r}")
        seen.add(key)
        out[key] = value
    return out


def _parse_json(text: str, where: str, problems: List[str]):
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=lambda c: (_ for _ in ()).throw(
                ValueError(f"非法 JSON 常量 {c}")),
        )
    except json.JSONDecodeError as exc:
        problems.append(f"{where}: JSON 解析失败（第{exc.lineno}行第{exc.colno}列）: {exc.msg}")
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
    return None


# --------------------------------------------------------------------------- #
# 列清单
# --------------------------------------------------------------------------- #
def load_column_list(path: str | Path,
                     problems: List[str]) -> Tuple[str, Tuple[str, ...]]:
    """读取列清单；问题写入 ``problems``，无法使用时返回 ``("", ())``。"""
    label = Path(path).name
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"{label}: 文件不可读: {exc}")
        return "", ()

    raw = _parse_json(text, label, problems)
    if raw is None:
        return "", ()
    if not isinstance(raw, dict):
        problems.append(f"{label}: 顶层必须是对象（含 columns 数组）")
        return "", ()

    unknown = set(raw) - _COLUMN_KEYS
    if unknown:
        problems.append(f"{label}: 顶层含未知字段 {sorted(unknown)}（仅允许 table_id/columns）")

    table_id = raw.get("table_id", "")
    if table_id is None:
        table_id = ""
    if not isinstance(table_id, str):
        problems.append(f"{label}: table_id 必须是字符串，得到 {type(table_id).__name__}")
        table_id = ""

    columns_raw = raw.get("columns")
    if not isinstance(columns_raw, list) or not columns_raw:
        problems.append(f"{label}: columns 必须是非空数组")
        return table_id, ()

    columns: List[str] = []
    first_seen: Dict[str, int] = {}
    for pos, item in enumerate(columns_raw, start=1):
        if not isinstance(item, str):
            problems.append(f"{label}: 第{pos}个列名不是字符串（得到 {type(item).__name__}）")
            continue
        if item == "":
            problems.append(f"{label}: 第{pos}个列名为空字符串")
            continue
        if item in first_seen:
            problems.append(
                f"{label}: 重复列名 {item!r}（第{first_seen[item]}个与第{pos}个）")
            continue
        first_seen[item] = pos
        columns.append(item)

    return table_id, tuple(columns)


# --------------------------------------------------------------------------- #
# 逐行文件
# --------------------------------------------------------------------------- #
def load_rows(path: str | Path, columns: Sequence[str],
              problems: List[str]) -> Tuple[str, ...]:
    """读取 JSONL 逐行文件并按列清单校验；返回 row_id 与值矩阵（未出错时）。"""
    label = Path(path).name
    column_list = list(columns)
    column_set = set(column_list)
    row_ids: List[str] = []
    values: List[Tuple[Optional[str], ...]] = []
    first_row_line: Dict[str, int] = {}
    row_index = 0

    try:
        handle = Path(path).open("r", encoding="utf-8")
    except OSError as exc:
        problems.append(f"{label}: 文件不可读: {exc}")
        return (), ()

    with handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row_index += 1
            where = f"{label} 第{line_no}行（第{row_index}条记录）"
            raw = _parse_json(line, where, problems)
            if raw is None:
                continue
            if not isinstance(raw, dict):
                problems.append(f"{where}: 记录必须是 JSON 对象")
                continue

            unknown = set(raw) - _ROW_KEYS
            if unknown:
                problems.append(f"{where}: 含未知字段 {sorted(unknown)}（仅允许 row_id/values）")

            row_id = raw.get("row_id")
            row_ok = True
            if not isinstance(row_id, str):
                detail = "缺失" if "row_id" not in raw else type(row_id).__name__
                problems.append(f"{where}: row_id 必须是字符串，得到 {detail}")
                row_ok = False
            elif row_id == "":
                problems.append(f"{where}: row_id 不能为空字符串")
                row_ok = False
            elif row_id in first_row_line:
                problems.append(
                    f"{where}: 重复 row_id {row_id!r}（首次出现在第{first_row_line[row_id]}行）")
                row_ok = False

            values_raw = raw.get("values")
            row_values: List[Optional[str]] = []
            if not isinstance(values_raw, dict):
                problems.append(f"{where}: values 必须是对象（列名 → 字符串/null）")
                row_ok = False
            else:
                missing = [c for c in column_list if c not in values_raw]
                extra = sorted(set(values_raw) - column_set)
                if missing:
                    problems.append(f"{where}: 少列 {missing}")
                    row_ok = False
                if extra:
                    problems.append(f"{where}: 多列 {extra}（列清单中不存在）")
                    row_ok = False
                for col in column_list:
                    if col not in values_raw:
                        row_values.append(None)
                        continue
                    value = values_raw[col]
                    if value is None or isinstance(value, str):
                        row_values.append(value)
                    else:
                        problems.append(
                            f"{where}: 列 {col!r} 的值类型为 {type(value).__name__}"
                            f"（只允许字符串或 null，得到 {value!r}）")
                        row_ok = False
                        row_values.append(None)

            if row_ok:
                first_row_line[row_id] = line_no
                row_ids.append(row_id)
                values.append(tuple(row_values))

    if problems:
        return (), ()
    return tuple(row_ids), tuple(values)


def load_dataset(columns_path: str | Path, rows_path: str | Path) -> Dataset:
    """读取并校验列清单 + 逐行文件；任何问题一次收集后抛 :class:`DepsInputError`。"""
    problems: List[str] = []
    table_id, columns = load_column_list(columns_path, problems)
    if columns:
        row_ids, values = load_rows(rows_path, columns, problems)
    else:
        row_ids, values = (), ()
    if problems:
        raise DepsInputError(problems)
    return Dataset(
        table_id=table_id,
        columns=columns,
        row_ids=row_ids,
        values=values,
        source=f"{Path(columns_path).name}+{Path(rows_path).name}",
    )


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
def validate_search_params(k: object, min_rows: object, column_count: int) -> Tuple[int, int]:
    """校验 k 与 min_rows；不合法时抛 :class:`DepsParameterError`。"""
    problems: List[str] = []
    if isinstance(k, bool) or not isinstance(k, int):
        problems.append(f"k 必须是整数，得到 {k!r}")
        k_int = -1
    else:
        k_int = k
    if isinstance(min_rows, bool) or not isinstance(min_rows, int):
        problems.append(f"min_rows 必须是整数，得到 {min_rows!r}")
        min_int = -1
    else:
        min_int = min_rows

    if column_count <= 0:
        problems.append("列清单为空，无法发现任何键或依赖")
    if not problems:
        if not (1 <= k_int <= column_count):
            problems.append(f"k 必须在 1..{column_count}（列数）之间，得到 {k_int}")
        if min_int < 2:
            problems.append(f"min_rows 必须是至少为 2 的整数，得到 {min_int}")
    if problems:
        raise DepsParameterError(problems)
    return k_int, min_int
