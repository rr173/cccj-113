"""依赖发现与规则核查的编排层：载入、搜索、规则核查、结果落盘/读取。

所有命令行都通过这里进入核心引擎。结果文件写到
``<store>/deps_calcs/<calc_id>.json``，失败（资料或参数不合法、规模超限）
同样落盘且带 ``status``，不产出任何「貌似成功」的发现列表。

本子系统为纯标准库实现（``array``/``json``/``hashlib``），不依赖
pm4py/pandas/scipy，也不使用生产搜索之外的第三方算法。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from .engine import (
    DEFAULT_MAX_SUBSETS,
    DiscoveryResult,
    RuleError,
    TableIndex,
    build_index,
    check_fd,
    check_fd_by_name,
    check_key,
    check_key_by_name,
    discover,
    resolve_columns,
)
from .io import (
    DepsInputError,
    DepsParameterError,
    ScaleExceeded,
    digest_files,
    load_dataset,
)
from .models import (
    DEFAULT_MAX_COLUMNS,
    DEFAULT_MAX_MAX_SCAN_CELLS,
    DEFAULT_MAX_ROWS,
    NULL_POLICIES,
    STATUS_HOLDS,
    Dataset,
)

CALC_DIR = "deps_calcs"

STATUS_OK = "OK"
STATUS_INVALID = "INVALID_INPUT"
STATUS_SCALE = "SCALE_EXCEEDED"


def _sanitize_calc_id(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text)


def _result_path(store_dir: str | Path, calc_id: str) -> Path:
    return Path(store_dir) / CALC_DIR / f"{calc_id}.json"


def _save(store_dir: str | Path, calc_id: str, payload: dict) -> None:
    path = _result_path(store_dir, calc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _scope_dict() -> dict:
    """显式声明本工具的规模范围；超出会提前拒绝，不标注为完整。"""
    return {
        "max_columns": DEFAULT_MAX_COLUMNS,
        "max_rows": DEFAULT_MAX_ROWS,
        "max_cells": DEFAULT_MAX_MAX_SCAN_CELLS,
        "max_subsets_to_k": DEFAULT_MAX_SUBSETS,
        "meaning": ("超出上述任一上限时直接拒绝（status=SCALE_EXCEEDED），"
                    "绝不输出被截断的「完整」发现。"),
    }


def _base_payload(columns_path, rows_path, k, min_rows, policy, calc_id) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calc_id": calc_id,
        "tool": "deps.discover",
        "input": {
            "columns_file": str(columns_path),
            "rows_file": str(rows_path),
        },
        "request": {"k": k, "min_rows": min_rows, "null_policy": policy},
        "scope": _scope_dict(),
        "semantics": {
            "null_policy_equal": "null 视为一种有别于任何字符串的共同值，全部行参与",
            "null_policy_ignore": ("每条规则单独选行：唯一键排除键列含 null 的行；"
                                   "X → A 排除 X 或 A 含 null 的行"),
            "min_rows": "参与行数低于 min_rows 即证据不足，不计为成立",
            "minimality": ("只相对于同口径、同 min_rows 下证据充足且成立的真子集判断"),
            "values": "字符串按字面比较：空字符串是实际值，不改写前导零/大小写/空格",
        },
    }


def _failure(store_dir, calc_id, base: dict, status: str, errors: List[str],
             note: str) -> dict:
    payload = {**base, "status": status, "errors": list(errors),
               "unique_keys": [], "functional_dependencies": [],
               "discovery_reported": False, "note": note}
    _save(store_dir, calc_id, payload)
    return payload


def discover_table(columns_path: str | Path, rows_path: str | Path,
                   k: int, min_rows: int, null_policy: str,
                   store_dir: str | Path,
                   calc_id: Optional[str] = None,
                   include_rows: bool = False) -> dict:
    """载入资料、求最小唯一键与最小函数依赖，并落盘结果。

    ``include_rows`` 控制是否在结果的 ``column_groups`` 中附完整 row_id 列表；
    默认只给摘要，避免大表的结果文件被解释性数据撑大。
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    calc_id = _sanitize_calc_id(calc_id or f"deps-{stamp}")
    base = _base_payload(columns_path, rows_path, k, min_rows, null_policy, calc_id)
    files = [Path(columns_path), Path(rows_path)]
    try:
        base["input"]["sha256"] = digest_files(files)
    except OSError as exc:
        return _failure(store_dir, calc_id, base, STATUS_INVALID,
                        [f"输入文件不可读: {exc}"], "资料不可读，未执行任何搜索。")

    if null_policy not in NULL_POLICIES:
        return _failure(store_dir, calc_id, base, STATUS_INVALID,
                        [f"未知空值口径 {null_policy!r}（应为 equal/ignore）"],
                        "参数不合法，未执行任何搜索。")

    try:
        dataset = load_dataset(columns_path, rows_path)
    except DepsInputError as exc:
        return _failure(store_dir, calc_id, base, STATUS_INVALID, exc.problems,
                        "资料不合法，整体拒绝；未跳过任何坏行，未产出任何发现。")

    try:
        index = build_index(dataset)
        result = discover(index, k, min_rows, null_policy)
    except (ScaleExceeded, DepsParameterError, RuleError) as exc:
        status = STATUS_SCALE if isinstance(exc, ScaleExceeded) else STATUS_INVALID
        return _failure(store_dir, calc_id, base, status, exc.problems,
                        "参数或规模不满足要求，未产出任何「完整」发现。")

    payload = _serialize_discovery(result, base, index, dataset, include_rows)
    _save(store_dir, calc_id, payload)
    return payload


def _column_group_summaries(index: TableIndex, include_rows: bool) -> list:
    """列等值分组的摘要；仅当显式要求时才附完整 row_id 列表。

    只统计前若干组的明细；完整分组随时可由 ``--include-column-rows`` 重新导出，
    避免大表的发现结果文件被"解释性数据"撑到几 MB。
    """
    out = []
    for group in index.column_indexes():
        sizes = []
        for i, value in enumerate(group.run_values):
            end = (group.run_starts[i + 1] if i + 1 < len(group.run_starts)
                   else len(group.rows))
            sizes.append((value, end - group.run_starts[i]))
        entry = {
            "column": index.columns[group.column],
            "distinct_non_null": len(group.run_values) - (1 if group.null_count else 0),
            "null_rows": group.null_count,
            "value_order": "Unicode 码位序（null 单独成段，排在字符串之前）",
            "distinct_groups": len(group.run_values),
            "largest_groups": [
                {"value": value, "row_count": count}
                for value, count in sorted(sizes, key=lambda x: (-x[1], x[0] is None,
                                                                 str(x[0])))[:10]
            ],
        }
        if include_rows:
            entry["groups"] = [
                {"value": value,
                 "row_count": sizes[i][1],
                 "row_ids": [index.row_ids[r] for r in group.rows[
                     group.run_starts[i]:(group.run_starts[i + 1]
                                          if i + 1 < len(group.run_starts)
                                          else len(group.rows))]]}
                for i, value in enumerate(group.run_values)
            ]
        out.append(entry)
    return out


def _serialize_discovery(result: DiscoveryResult, base: dict,
                         index: TableIndex, dataset: Dataset,
                         include_rows: bool = False) -> dict:
    payload = {
        **base,
        "status": STATUS_OK,
        "table_id": result.table_id,
        "row_count": result.total_rows,
        "column_count": index.column_count,
        "columns": list(result.columns),
        "complete": True,
        "claimable": result.claimable,
        "params_valid": True,
        "unique_keys": [k.as_dict() for k in result.keys],
        "functional_dependencies": [f.as_dict() for f in result.fds],
        "evidence_gaps": list(result.evidence_gaps),
        "stats": dict(result.stats),
        "notes": list(result.notes),
        "column_groups": _column_group_summaries(index, include_rows),
        "column_groups_complete": bool(include_rows),
        "limitations": [
            "只研究一张表内字段值的精确对应关系；不做实体合并、不分析加工先后、不做统计估计。",
            "成立不等于供应商的永久承诺：换一批资料、换口径或提高 min_rows 都可能改变结论。",
            "所有比较按字面进行：前导零、大小写与空格不会被改写。",
        ],
    }
    return payload


def load_dataset_and_index(columns_path, rows_path) -> TableIndex:
    dataset = load_dataset(columns_path, rows_path)
    return build_index(dataset)


def check_rule(columns_path: str | Path, rows_path: str | Path,
               k: int, min_rows: int, null_policy: str,
               *, key_columns: Optional[Sequence[str]] = None,
               left: Optional[Sequence[str]] = None, right: Optional[str] = None,
               store_dir: Optional[str | Path] = None,
               calc_id: Optional[str] = None) -> dict:
    """核查一条拟定规则（唯一键或任意单右部依赖）。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    if calc_id is not None and store_dir is not None:
        calc_id = _sanitize_calc_id(calc_id)
    elif store_dir is not None:
        calc_id = _sanitize_calc_id(f"check-{stamp}")

    if key_columns is not None and (left is not None or right is not None):
        raise RuleError(["一次只能核查一条规则：要么给键列，要么给 X → A，不能同时给"])
    if key_columns is None and right is None:
        raise RuleError(["必须给出 --key 或 (--left... 与 --right)"])

    base = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calc_id": calc_id,
        "tool": "deps.check",
        "input": {"columns_file": str(columns_path), "rows_file": str(rows_path)},
        "request": {
            "k": k,
            "min_rows": min_rows,
            "null_policy": null_policy,
            "rule": ({"kind": "unique_key", "columns": list(key_columns)}
                     if key_columns is not None else
                     {"kind": "functional_dependency",
                      "left": list(left or ()), "right": right}),
        },
        "scope": _scope_dict(),
        "semantics": {
            "null_policy_ignore": ("唯一键排除键列含 null 的行；X → A 排除 X 或 A 含 null 的行"),
            "min_rows": "参与行数低于 min_rows 即证据不足，不伪造反例",
            "counterexample_order": ("违反时取 (row_id_a, row_id_b) 字典序最小的那一对，"
                                     "row_id 按 Unicode 码位序比较"),
        },
    }

    def fail(status: str, errors: List[str], note: str) -> dict:
        payload = {**base, "status": status, "errors": list(errors),
                   "verdict": None, "counterexample": None, "note": note}
        if store_dir is not None:
            _save(store_dir, calc_id, payload)
        return payload

    try:
        base["input"]["sha256"] = digest_files([columns_path, rows_path])
    except OSError as exc:
        return fail(STATUS_INVALID, [f"输入文件不可读: {exc}"], "资料不可读，未核查。")

    if null_policy not in NULL_POLICIES:
        return fail(STATUS_INVALID,
                    [f"未知空值口径 {null_policy!r}（应为 equal/ignore）"],
                    "参数不合法，未核查。")
    try:
        dataset = load_dataset(columns_path, rows_path)
        index = build_index(dataset)
    except DepsInputError as exc:
        return fail(STATUS_INVALID, exc.problems, "资料不合法，整体拒绝核查。")
    except ScaleExceeded as exc:
        return fail(STATUS_SCALE, exc.problems, "规模超出已声明范围，拒绝核查。")

    try:
        if key_columns is not None:
            result = check_key_by_name(index, key_columns, k, null_policy, min_rows)
        else:
            result = check_fd_by_name(index, list(left or ()), right, k, null_policy,
                                      min_rows)
    except (RuleError, DepsParameterError, ScaleExceeded) as exc:
        return fail(STATUS_INVALID, exc.problems, "规则或参数不合法，未核查。")

    payload = {**base, "status": STATUS_OK, **result,
               "table_id": index.table_id,
               "row_count": index.row_count,
               "columns": list(index.columns)}
    if (result.get("verdict") == "VIOLATED" and key_columns is None
            and right is not None):
        payload["explanation"] = _explain_payload(index, list(left or ()), right, k,
                                                  min_rows, null_policy)
    if store_dir is not None:
        _save(store_dir, calc_id, payload)
    return payload


def _explain_payload(index: TableIndex, left: Sequence[str], right: str, k: int,
                     min_rows: int, null_policy: str) -> dict:
    """解释一条违反的 X → A：加列的核查结果与右部 A 真正的最小左部。

    两者都基于**原值**：不做任何取值改写、不做近似匹配。加列成立不是靠
    改掉右部取值，而是新增列把冲突记录分到不同左部（或因筛行而证据不足）。
    """
    left_pos = resolve_columns(index, left)
    right_pos = resolve_columns(index, [right])[0]
    if right_pos in left_pos:
        raise RuleError([f"右部列 {right!r} 不能同时出现在左部"])
    extensions: List[dict] = []
    if len(left_pos) < k:
        for col in range(index.column_count):
            if col in left_pos or col == right_pos:
                continue
            new_left = list(left_pos) + [col]
            evidence, violation = check_fd(index, new_left, right_pos, null_policy,
                                           min_rows)
            extensions.append({
                "added_column": index.columns[col],
                "left": [index.columns[c] for c in new_left],
                "check": evidence.as_dict(),
                "verdict": evidence.status,
                "counterexample": (violation.as_dict(index.column_names(new_left),
                                                     index.columns[right_pos])
                                   if violation is not None else None),
            })
    discovered = discover(index, k, min_rows, null_policy)
    minimal_lefts = [
        {"left": list(dep.left),
         "expression": dep.as_dict()["expression"],
         "check": dep.evidence.as_dict()}
        for dep in sorted((d for d in discovered.fds if d.right == right),
                          key=lambda d: (len(d.left), d.left))
    ]
    return {
        "checked_rule": "{} → {}".format(
            "∅" if not left else ", ".join(left), right),
        "extensions": extensions,
        "minimal_lefts_for_right": minimal_lefts,
        "repaired_by_values": False,
        "explanation": (
            "加列后成立并不需要改掉任何原值：冲突记录仍在，只是新增列把它们分到"
            "不同左部；也可能是参与行被筛少到证据不足（见 check.status）。"
            "本资料中右部列的真正最小左部见 minimal_lefts_for_right。"),
    }


def explain_rule(columns_path: str | Path, rows_path: str | Path,
                 left: Sequence[str], right: str, k: int, min_rows: int,
                 null_policy: str) -> dict:
    """载入资料并解释一条违反的依赖（供 CLI ``deps-explain`` 使用）。"""
    index = load_dataset_and_index(columns_path, rows_path)
    return _explain_payload(index, left, right, k, min_rows, null_policy)


def load_result(store_dir: str | Path, calc_id: str) -> dict:
    path = _result_path(store_dir, calc_id)
    if not path.exists():
        raise KeyError(f"结果不存在: {calc_id}")
    return json.loads(path.read_text(encoding="utf-8"))
