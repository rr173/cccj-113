"""单表依赖发现与规则核查的测试。

覆盖要点：

1. 生产搜索 vs 独立小表枚举器：随机小表上逐项比对全部最小结果与证据缺口；
2. 专门构造的边界表：组合键、空值口径分歧、空左部常量依赖、整行重复、
   ignore 下「成立 → 加列后证据不足」；
3. 违反反例：最小 row 对、加列后成立、真正最小左部、不靠改值；
4. 打乱资料顺序后结果与反例完全一致；
5. 资料错误定位到行/列，修好后可重跑；
6. 非法 k / min_rows 不产出「貌似成功」的发现；
7. 规模超限提前拒绝且不标为完整；
8. 值域按字面比较（空字符串、前导零、大小写、空格）。
"""
from __future__ import annotations

import itertools
import json
import random
from pathlib import Path

import pytest

from mes_pipeline.deps.bruteforce import check_fd as bf_check_fd
from mes_pipeline.deps.bruteforce import check_key as bf_check_key
from mes_pipeline.deps.bruteforce import discover_all
from mes_pipeline.deps.engine import (
    build_index,
    check_fd,
    check_key,
    discover,
)
from mes_pipeline.deps.io import (
    DepsInputError,
    DepsParameterError,
    ScaleExceeded,
    load_dataset,
)
from mes_pipeline.deps.models import Dataset
from mes_pipeline.deps import service


def make_dataset(columns, rows, table_id="t"):
    row_ids = tuple(f"R{i:05d}" for i in range(len(rows)))
    return Dataset(table_id=table_id, columns=tuple(columns), row_ids=row_ids,
                   values=tuple(tuple(r) for r in rows))


def result_columns(result):
    d = result.as_dict()
    return (sorted(tuple(x["columns"]) for x in d["unique_keys"]),
            sorted((tuple(x["left"]), x["right"])
                   for x in d["functional_dependencies"]),
            sorted((g["kind"], tuple(g.get("left", g.get("columns", ()))), g.get("right", ""))
                   for g in d["evidence_gaps"]))


# --------------------------------------------------------------------------- #
# 1. 与独立枚举器的完备性核对
# --------------------------------------------------------------------------- #
def _normalize_gaps(gaps):
    return sorted(
        (g["kind"], tuple(g.get("left", g.get("columns", ()))), g.get("right", ""))
        for g in gaps
    )


@pytest.mark.parametrize("seed", [11, 12])
def test_random_small_tables_match_bruteforce_strict(seed):
    rng = random.Random(seed)
    columns = ("a", "b", "c", "d")
    domains = [["x", "y", None, "", "X"] for _ in columns]
    for _ in range(150):
        n = rng.randint(0, 9)
        rows = [[rng.choice(domains[c]) for c in range(4)] for _ in range(n)]
        ds = make_dataset(columns, rows)
        index = build_index(ds)
        for policy in ("equal", "ignore"):
            for k in (1, 2, 3):
                for min_rows in (2, 3, 12):
                    result = discover(index, k, min_rows, policy).as_dict()
                    brute = discover_all(columns, ds.row_ids, ds.values,
                                         k, min_rows, policy)
                    assert sorted(tuple(x["columns"]) for x in result["unique_keys"]) == \
                        sorted(tuple(x["columns"]) for x in brute["unique_keys"])
                    assert sorted((tuple(x["left"]), x["right"])
                                  for x in result["functional_dependencies"]) == \
                        sorted((tuple(x["left"]), x["right"])
                               for x in brute["functional_dependencies"])
                    assert _normalize_gaps(result["evidence_gaps"]) == \
                        _normalize_gaps(brute["evidence_gaps"])


def test_check_results_and_counterexamples_match_bruteforce():
    rng = random.Random(21)
    columns = ("a", "b", "c")
    domains = [["x", "y", None, ""] for _ in columns]
    for _ in range(150):
        n = rng.randint(1, 9)
        rows = [[rng.choice(domains[c]) for c in range(3)] for _ in range(n)]
        ds = make_dataset(columns, rows)
        index = build_index(ds)
        for policy in ("equal", "ignore"):
            for min_rows in (2, 3):
                for subset in itertools.chain(
                        itertools.combinations(range(3), 1),
                        itertools.combinations(range(3), 2)):
                    ev, viol = check_key(index, subset, policy, min_rows)
                    brute = bf_check_key(ds.values, ds.row_ids, subset, policy, min_rows)
                    assert ev.status == brute["status"]
                    assert ev.participating == brute["participating"]
                    if brute["counterexample"] is None:
                        assert viol is None
                    else:
                        got = viol.as_dict([columns[c] for c in subset])
                        for key in ("row_id_a", "row_id_b", "left_values_a",
                                    "left_values_b"):
                            assert got[key] == brute["counterexample"][key]
                for left in itertools.chain([()], itertools.combinations(range(3), 1),
                                            itertools.combinations(range(3), 2)):
                    for right in range(3):
                        if right in left:
                            continue
                        ev, viol = check_fd(index, left, right, policy, min_rows)
                        brute = bf_check_fd(ds.values, ds.row_ids, left, right,
                                            policy, min_rows)
                        assert ev.status == brute["status"]
                        assert ev.participating == brute["participating"]
                        if brute["counterexample"] is None:
                            assert viol is None
                        else:
                            got = viol.as_dict([columns[c] for c in left],
                                               columns[right])
                            for key in ("row_id_a", "row_id_b", "left_values_a",
                                        "left_values_b", "right_value_a",
                                        "right_value_b"):
                                assert got[key] == brute["counterexample"][key]
