"""与生产搜索无关的小表枚举器（独立参考实现，供测试与复核脚本使用）。

刻意 **不导入** :mod:`mes_pipeline.deps.engine` / ``service`` / ``models``：
本模块只接收 `(列名, row_id 列表, 值矩阵)`，用最直白的方式重新实现口径：

- 对每个候选组合独立重算参与行集合（不共享索引、不做增量标签）；
- 唯一键用「参与行的值元组集合大小 == 参与行数」判定；
- 函数依赖用「以左部为键的字典里右部值是否唯一」判定，忽略项目中的
  单调性剪枝与位图；
- 反例按 ``(row_id_a, row_id_b)`` 字典序枚举**全部**行对后取最小；
- 最小性用「所有真子集重新核查」定义，不引用生产代码的任何结论。

因此它只适合小表（行对枚举是 O(n²)），在测试中用来逐项核对生产结果是否
完整、是否正确。
"""
from __future__ import annotations

from itertools import combinations
from typing import Dict, List, Optional, Sequence, Tuple

HOLDS = "HOLDS"
VIOLATED = "VIOLATED"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"


def _participating(values, cols, policy):
    """返回参与行序号列表（按 row_id Unicode 顺序给出前由调用方保证顺序）。"""
    rows = []
    for i, row in enumerate(values):
        if policy == "ignore":
            if any(row[c] is None for c in cols):
                continue
        rows.append(i)
    return rows


def check_key(values, row_ids, cols, policy: str, min_rows: int):
    rows = _participating(values, cols, policy)
    participating = len(rows)
    groups: Dict[tuple, List[int]] = {}
    for i in rows:
        groups.setdefault(tuple(values[i][c] for c in cols), []).append(i)
    conflicts = [members for members in groups.values() if len(members) > 1]
    violation = None
    if conflicts:
        pairs = []
        for members in conflicts:
            for a in members:
                for b in members:
                    if a < b:
                        pairs.append((row_ids[a], row_ids[b], a, b))
        pairs.sort()
        _, _, a, b = pairs[0]
        violation = {
            "row_id_a": row_ids[a], "row_id_b": row_ids[b],
            "left_values_a": [values[a][c] for c in cols],
            "left_values_b": [values[b][c] for c in cols],
        }
    if conflicts:
        status = VIOLATED
    elif participating < min_rows:
        status = INSUFFICIENT
    else:
        status = HOLDS
    return {"status": status, "participating": participating,
            "excluded": len(values) - participating,
            "counterexample": violation}


def check_fd(values, row_ids, left, right, policy: str, min_rows: int):
    cols = list(left) + [right]
    rows = _participating(values, cols, policy)
    participating = len(rows)
    groups: Dict[tuple, List[int]] = {}
    for i in rows:
        groups.setdefault(tuple(values[i][c] for c in left), []).append(i)
    pairs = []
    for members in groups.values():
        for a, b in combinations(members, 2):
            if values[a][right] != values[b][right]:
                pairs.append((row_ids[a], row_ids[b], a, b))
    violation = None
    if pairs:
        pairs.sort()
        _, _, a, b = pairs[0]
        violation = {
            "row_id_a": row_ids[a], "row_id_b": row_ids[b],
            "left_values_a": [values[a][c] for c in left],
            "left_values_b": [values[b][c] for c in left],
            "right_value_a": values[a][right],
            "right_value_b": values[b][right],
        }
    if pairs:
        status = VIOLATED
    elif participating < min_rows:
        status = INSUFFICIENT
    else:
        status = HOLDS
    return {"status": status, "participating": participating,
            "excluded": len(values) - participating,
            "counterexample": violation}


def _minimal(cols: int, left, policy: str, min_rows: int, kind: str,
             values, row_ids, right: Optional[int] = None) -> bool:
    """检查所有「更小」的左部是否已经「成立」。"""
    if kind == "key":
        for size in range(1, len(left)):
            for sub in combinations(left, size):
                if check_key(values, row_ids, sub, policy, min_rows)["status"] == HOLDS:
                    return False
        return True
    for size in range(0, len(left)):
        for sub in combinations(left, size):
            if check_fd(values, row_ids, sub, right, policy, min_rows)["status"] == HOLDS:
                return False
    return True


def discover_all(columns: Sequence[str], row_ids: Sequence[str], values,
                 k: int, min_rows: int, policy: str,
                 empty_left_scope: bool = False) -> dict:
    """枚举 1..k 的全部组合，返回与生产结果同构的字典（供逐项比对）。

    ``empty_left_scope`` 的含义与生产引擎相同：是否把空左部算作「更小的无冲突
    候选」来压缩 ``evidence_gaps``。
    """
    n = len(columns)
    keys: List[dict] = []
    fds: List[dict] = []
    gaps: List[dict] = []
    for size in range(1, k + 1):
        for subset in combinations(range(n), size):
            result = check_key(values, row_ids, subset, policy, min_rows)
            if result["status"] == HOLDS and _minimal(n, subset, policy, min_rows,
                                                      "key", values, row_ids):
                keys.append({"columns": [columns[c] for c in subset],
                             "check": result})
            elif (result["status"] == INSUFFICIENT
                  and not any(check_key(values, row_ids, sub, policy, min_rows)["status"] != VIOLATED
                              for sz in range(1, len(subset))
                              for sub in combinations(subset, sz))):
                gaps.append({"kind": "unique_key",
                             "columns": [columns[c] for c in subset],
                             "check": result})
    for right in range(n):
        for size in range(0, k + 1):
            for left in combinations([c for c in range(n) if c != right], size):
                result = check_fd(values, row_ids, left, right, policy, min_rows)
                if result["status"] == HOLDS and _minimal(n, left, policy, min_rows,
                                                          "fd", values, row_ids, right):
                    fds.append({"left": [columns[c] for c in left],
                                "right": columns[right], "check": result})
                elif result["status"] == INSUFFICIENT and not any(
                        check_fd(values, row_ids, sub, right, policy, min_rows)["status"] != VIOLATED
                        for sz in range(0 if empty_left_scope else 1, len(left))
                        for sub in combinations(left, sz)):
                    gaps.append({"kind": "functional_dependency",
                                 "left": [columns[c] for c in left],
                                 "right": columns[right], "check": result})
    return {"unique_keys": keys, "functional_dependencies": fds, "evidence_gaps": gaps}
