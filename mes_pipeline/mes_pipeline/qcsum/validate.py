"""输入约定与校验：严格 JSON、行/操作/批次校验、批次内容规范化哈希。

约定（违反一律拒绝，不猜测、不补全）：

- JSON 对象键不允许重复（`loads_strict` 对任何层级的重复键都拒绝）；
- 行必须恰好包含本表全部列，缺列、多列都拒绝；
- lot_id / sample_id / supplier / site 为非空字符串，按原文相等；
- grade 为字符串或 null，空字符串是有实际含义的值；
- rejected_units 为 0..1000000 的整数或 null，布尔值不当整数；
- 批次必须且只能包含 batch_id（非空字符串）与 ops（有序数组）；
- 每个操作必须且只能包含 op/table/key（insert/update 另有 row）；
- insert/update 的 row 主键必须等于 key（主键本身不可改）。
"""
from __future__ import annotations

import hashlib
import json

from .errors import QcsumValidation

LOTS_COLUMNS = ("lot_id", "supplier", "site")
SAMPLES_COLUMNS = ("sample_id", "lot_id", "grade", "rejected_units")
TABLE_COLUMNS = {"lots": LOTS_COLUMNS, "samples": SAMPLES_COLUMNS}
TABLE_PK = {"lots": "lot_id", "samples": "sample_id"}
TABLES = tuple(TABLE_COLUMNS)
OP_NAMES = ("insert", "update", "delete")
MAX_REJECTED_UNITS = 1_000_000


def _no_dup_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise QcsumValidation(f"JSON 对象键重复: {key!r}")
        obj[key] = value
    return obj


def loads_strict(text):
    """解析 JSON 文本；任何层级的重复对象键都拒绝。"""
    try:
        return json.loads(text, object_pairs_hook=_no_dup_object)
    except json.JSONDecodeError as exc:
        raise QcsumValidation(f"JSON 解析失败: {exc}") from None


def _is_nonempty_str(value):
    return isinstance(value, str) and value != ""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_row(table, row, where=""):
    """校验一行原表数据，返回原 dict。where 用于错误定位（如 'ops[2]'）。"""
    prefix = f"{where}: " if where else ""
    if not isinstance(row, dict):
        raise QcsumValidation(f"{prefix}{table} 行必须是 JSON 对象，得到 {type(row).__name__}")
    cols = TABLE_COLUMNS[table]
    keys = set(row)
    missing = [c for c in cols if c not in keys]
    extra = sorted(k for k in row if k not in cols)
    if missing:
        raise QcsumValidation(f"{prefix}{table} 行缺列 {missing}")
    if extra:
        raise QcsumValidation(f"{prefix}{table} 行多列 {extra}")
    if table == "lots":
        for field in cols:
            if not _is_nonempty_str(row[field]):
                raise QcsumValidation(
                    f"{prefix}lots.{field} 必须是非空字符串，得到 {row[field]!r}")
    else:
        if not _is_nonempty_str(row["sample_id"]):
            raise QcsumValidation(
                f"{prefix}samples.sample_id 必须是非空字符串，得到 {row['sample_id']!r}")
        if not _is_nonempty_str(row["lot_id"]):
            raise QcsumValidation(
                f"{prefix}samples.lot_id 必须是非空字符串，得到 {row['lot_id']!r}")
        grade = row["grade"]
        if grade is not None and not isinstance(grade, str):
            raise QcsumValidation(
                f"{prefix}samples.grade 必须是字符串或 null，得到 {grade!r}")
        rejected = row["rejected_units"]
        if rejected is not None:
            if not _is_int(rejected):
                raise QcsumValidation(
                    f"{prefix}samples.rejected_units 必须是整数或 null"
                    f"（布尔值不算整数），得到 {rejected!r}")
            if not 0 <= rejected <= MAX_REJECTED_UNITS:
                raise QcsumValidation(
                    f"{prefix}samples.rejected_units 超出 0..{MAX_REJECTED_UNITS}，"
                    f"得到 {rejected!r}")
    return row


def _validate_op(index, op):
    where = f"ops[{index}]"
    if not isinstance(op, dict):
        raise QcsumValidation(f"{where}: 操作必须是 JSON 对象")
    kind = op.get("op")
    if kind not in OP_NAMES:
        raise QcsumValidation(f"{where}.op 必须是 insert/update/delete，得到 {kind!r}")
    allowed = {"op", "table", "key"} | ({"row"} if kind in ("insert", "update") else set())
    keys = set(op)
    if keys != allowed:
        missing = sorted(allowed - keys)
        extra = sorted(keys - allowed)
        raise QcsumValidation(
            f"{where}: 字段必须且只能是 {sorted(allowed)}；缺 {missing}，多 {extra}")
    table = op["table"]
    if table not in TABLE_COLUMNS:
        raise QcsumValidation(f"{where}.table 必须是 lots 或 samples，得到 {table!r}")
    key = op["key"]
    if not _is_nonempty_str(key):
        raise QcsumValidation(f"{where}.key 必须是非空字符串，得到 {key!r}")
    norm = {"op": kind, "table": table, "key": key}
    if kind in ("insert", "update"):
        row = validate_row(table, op["row"], where=where)
        pk = TABLE_PK[table]
        if row[pk] != key:
            raise QcsumValidation(
                f"{where}: 主键本身不可改，key={key!r} 与 row.{pk}={row[pk]!r} 不一致")
        norm["row"] = row
    return norm


def validate_batch(obj):
    """校验更改批次，返回 (batch_id, 规范化操作列表)。"""
    if not isinstance(obj, dict):
        raise QcsumValidation("更改批次必须是 JSON 对象")
    keys = set(obj)
    if keys != {"batch_id", "ops"}:
        missing = sorted({"batch_id", "ops"} - keys)
        extra = sorted(keys - {"batch_id", "ops"})
        raise QcsumValidation(
            f"批次必须且只能包含 batch_id 与 ops；缺 {missing}，多 {extra}")
    batch_id = obj["batch_id"]
    if not _is_nonempty_str(batch_id):
        raise QcsumValidation(f"batch_id 必须是非空字符串，得到 {batch_id!r}")
    ops = obj["ops"]
    if not isinstance(ops, list):
        raise QcsumValidation("ops 必须是有序数组（数组次序有意义）")
    return batch_id, [_validate_op(i, op) for i, op in enumerate(ops)]


def canonical_hash(batch_obj):
    """批次内容的规范化哈希：对象键次序无关，操作列表次序有关。"""
    try:
        text = json.dumps(batch_obj, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise QcsumValidation(f"批次内容无法规范化序列化: {exc}") from None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
