"""依赖发现的场景测试：边界表、空值口径、最小性摘要、乱序不变性。"""
from __future__ import annotations

import itertools
import random

from mes_pipeline.deps.engine import build_index, check_fd, check_key, discover
from mes_pipeline.deps.models import Dataset


def make_dataset(columns, rows, row_ids=None):
    if row_ids is None:
        row_ids = tuple(f"R{i:05d}" for i in range(len(rows)))
    return Dataset(table_id="t", columns=tuple(columns), row_ids=tuple(row_ids),
                   values=tuple(tuple(r) for r in rows))


def keys_of(result):
    return sorted(tuple(x["columns"]) for x in result.as_dict()["unique_keys"])


def fds_of(result):
    return sorted((tuple(x["left"]), x["right"])
                  for x in result.as_dict()["functional_dependencies"])


# --------------------------------------------------------------------------- #
# 组合键：二列唯一，各单列都不唯一
# --------------------------------------------------------------------------- #
def test_two_column_key_made_of_two_non_unique_columns():
    columns = ("supplier", "part", "name")
    rows = [
        ["S1", "P-01", "轴承座"],
        ["S1", "P-02", "端盖"],
        ["S2", "P-01", "轴承座"],
        ["S2", "P-03", "轴套"],
    ]
    index = build_index(make_dataset(columns, rows))
    result = discover(index, 2, 2, "equal")
    assert ("supplier", "part") in keys_of(result)
    # 两个单列都不是键：各有一个重复值
    assert ("supplier",) not in keys_of(result)
    assert ("part",) not in keys_of(result)
    assert not any(len(cols) == 1 for cols in keys_of(result))


def test_composite_key_minimality_summaries_show_real_counterexamples():
    columns = ("supplier", "part", "name")
    rows = [
        ["S1", "P-01", "轴承座"],
        ["S1", "P-02", "端盖"],
        ["S2", "P-01", "轴承座"],
        ["S2", "P-03", "轴套"],
    ]
    index = build_index(make_dataset(columns, rows))
    result = discover(index, 2, 2, "equal")
    key = next(k for k in result.keys if len(k.columns) == 2)
    assert {d.deleted_column for d in key.delete_summaries} == {"supplier", "part"}
    for summary in key.delete_summaries:
        assert summary.evidence.status == "VIOLATED"
        assert summary.violation is not None
        # 反例的两行在被删列上确实同值
        assert summary.violation.left_a == summary.violation.left_b


# --------------------------------------------------------------------------- #
# 空值口径：同一依赖两种结论
# --------------------------------------------------------------------------- #
NULL_TABLE = (
    ("supplier", "part", "name"),
    [
        ["S1", "P-01", "轴承座"],
        ["S1", "P-02", "轴承座"],
        ["S2", "P-03", "端盖"],
        ["S2", "P-04", None],
        ["S3", "P-05", "垫圈"],
    ],
)


def test_null_policies_disagree_on_same_dependency():
    columns, rows = NULL_TABLE
    index = build_index(make_dataset(columns, rows))
    left, right = (0,), 2
    ev_equal, viol_equal = check_fd(index, left, right, "equal", 2)
    ev_ignore, viol_ignore = check_fd(index, left, right, "ignore", 2)
    assert ev_equal.status == "VIOLATED"
    assert viol_equal is not None
    assert (viol_equal.right_a, viol_equal.right_b) == ("端盖", None)
    assert ev_ignore.status == "HOLDS"
    assert viol_ignore is None
    # 参与/排除数明确区分：equal 全部参与，ignore 排除含 null 的行
    assert ev_equal.participating == 5
    assert ev_equal.excluded_null == 0
    assert ev_ignore.participating == 4
    # 被排除的是「右部为 null」的那一行（左部 supplier 没有 null）
    assert ev_ignore.excluded_null_left == 0
    assert ev_ignore.excluded_null_right == 1


def test_null_key_policy_excludes_rows_containing_null():
    columns = ("a", "b")
    rows = [["x", None], ["y", None], ["z", "1"]]
    index = build_index(make_dataset(columns, rows))
    # 键取 b 列：equal 下两行 null 视为同一值 → 不是键；
    # ignore 下含 null 的两行被排除，只剩 1 行 → 证据不足（而不是「成立」）。
    ev_equal, viol = check_key(index, (1,), "equal", 2)
    ev_ignore, _ = check_key(index, (1,), "ignore", 2)
    assert ev_equal.status == "VIOLATED"
    assert ev_equal.participating == 3
    assert viol is not None
    assert ev_ignore.status == "INSUFFICIENT_EVIDENCE"
    assert ev_ignore.participating == 1
    assert ev_ignore.excluded_null_left == 2


# --------------------------------------------------------------------------- #
# 空左部常量依赖
# --------------------------------------------------------------------------- #
def test_empty_left_constant_dependency_is_found_and_minimal():
    columns = ("supplier", "origin", "unit")
    rows = [
        ["S1", "CN", "mm"],
        ["S2", "CN", "mm"],
        ["S3", "CN", "cm"],
    ]
    index = build_index(make_dataset(columns, rows))
    result = discover(index, 2, 2, "equal")
    fds = fds_of(result)
    assert ((), "origin") in fds
    assert ((), "unit") not in fds  # 非常量列
    const = next(f for f in result.fds if not f.left and f.right == "origin")
    assert const.delete_summaries == ()  # 空左部没有待删列
    # 常量列不是键：所有行同值，唯一键要求两两不同
    assert ("origin",) not in keys_of(result)


# --------------------------------------------------------------------------- #
# 整行重复（row_id 不同）
# --------------------------------------------------------------------------- #
def test_duplicate_whole_rows_prevent_any_key_within_k():
    columns = ("supplier", "part", "name", "grade")
    rows = [
        ["S1", "P-01", "轴承座", "A"],
        ["S1", "P-02", "端盖", "A"],
        ["S2", "P-01", "轴承座", "B"],
        ["S1", "P-02", "端盖", "A"],  # 与第 2 条整行相同，只有 row_id 不同
    ]
    index = build_index(make_dataset(columns, rows))
    for policy in ("equal", "ignore"):
        result = discover(index, 4, 2, policy)
        assert result.keys == []
        # 但函数依赖仍可发现：整行重复不排除 FD
        assert ("part",) in [tuple(f.left) for f in result.fds]


# --------------------------------------------------------------------------- #
# ignore：成立 → 加列后证据不足
# --------------------------------------------------------------------------- #
GAP_TABLE = (
    ("supplier", "batch", "name"),
    [
        ["S1", "L1", "轴承座"],
        ["S1", None, "轴承座"],
        ["S2", None, "端盖"],
        ["S2", None, "端盖"],
        ["S3", None, "垫圈"],
    ],
)


def test_ignore_gap_dependency_holds_then_becomes_insufficient():
    columns, rows = GAP_TABLE
    index = build_index(make_dataset(columns, rows))
    small, _ = check_fd(index, (0,), 2, "ignore", 2)   # supplier → name
    bigger, _ = check_fd(index, (0, 1), 2, "ignore", 2)  # supplier, batch → name
    assert small.status == "HOLDS"
    assert small.participating == 5
    assert bigger.status == "INSUFFICIENT_EVIDENCE"
    assert bigger.participating == 1  # 只有 S1/L1 一行同时非 null
    result = discover(index, 2, 2, "ignore")
    gaps = {(tuple(g.get("left", ())), g.get("right"))
            for g in result.as_dict()["evidence_gaps"]
            if g["kind"] == "functional_dependency"}
    # 最简洁的证据缺口是 batch → name 本身（只有 1 行参与）；
    # supplier,batch → name 有更小的无冲突左部可解释，因此不重复列出。
    assert (("batch",), "name") in gaps
    # 「证据不足」不得混进成立结果
    assert not any("batch" in left for left, _right in fds_of(result))


def test_gap_is_not_reported_under_equal_when_rows_are_plentiful():
    columns, rows = GAP_TABLE
    index = build_index(make_dataset(columns, rows))
    ev, _ = check_fd(index, (0, 1), 2, "equal", 2)
    # equal 口径下 null 参与比较，参与行数仍是 5，因此靠计数不会退化成
    # 「证据不足」；这条组合在本资料上恰好成立，也就不属于证据缺口。
    assert ev.participating == 5
    result = discover(index, 2, 2, "equal")
    gaps = {(tuple(g.get("left", ())), g.get("right"))
            for g in result.as_dict()["evidence_gaps"]
            if g["kind"] == "functional_dependency"}
    assert (("supplier", "batch"), "name") not in gaps


# --------------------------------------------------------------------------- #
# 最小性：不得把「更小成立」的组合也列出来
# --------------------------------------------------------------------------- #
def test_minimality_excludes_supersets_of_holding_subsets():
    columns = ("a", "b", "c")
    rows = [["1", "x", "p"], ["2", "x", "p"], ["3", "y", "q"]]
    index = build_index(make_dataset(columns, rows))
    result = discover(index, 3, 2, "equal")
    fds = fds_of(result)
    assert the_smaller_holds(fds)
    # 单列唯一键成立时，它的任何超集都不该列为最小键
    assert ("a",) in keys_of(result)
    assert ("a", "b") not in keys_of(result)
    assert ("a", "c") not in keys_of(result)


def the_smaller_holds(fds):
    names = {left for left, right in fds}
    return () in names or ("b",) in names


# --------------------------------------------------------------------------- #
# 违反：反例最小、加列后成立、原始取值不变
# --------------------------------------------------------------------------- #
VIOLATION_TABLE = (
    ("supplier", "part", "name", "origin"),
    [
        ["S1", "P-01", "轴承座", "CN"],
        ["S1", "P-02", "端盖", "CN"],
        ["S2", "P-01", "轴承座", "CN"],
        ["S2", "P-03", "轴套", "CN"],
        ["S3", "P-04", "垫圈", "CN"],
    ],
)


def test_violation_counterexample_is_minimal_row_pair_and_values_untouched():
    columns, rows = VIOLATION_TABLE
    index = build_index(make_dataset(columns, rows, row_ids=tuple(
        f"V-{i:03d}" for i in range(1, len(rows) + 1))))
    ev, viol = check_fd(index, (0,), 2, "equal", 2)
    assert ev.status == "VIOLATED"
    assert viol.row_id_a < viol.row_id_b
    assert viol.left_a == viol.left_b == ("S1",)
    assert (viol.right_a, viol.right_b) == ("轴承座", "端盖")
    # 原始值逐字保留，未被改写
    assert viol.right_a in {row[2] for row in rows}
    assert viol.right_b in {row[2] for row in rows}


def test_violation_holds_after_adding_column_without_renaming_values():
    columns, rows = VIOLATION_TABLE
    index = build_index(make_dataset(columns, rows))
    before, viol = check_fd(index, (0,), 2, "equal", 2)
    after, viol_after = check_fd(index, (0, 1), 2, "equal", 2)
    assert before.status == "VIOLATED"
    assert viol is not None
    assert after.status == "HOLDS"
    assert viol_after is None
    # 依赖左侧扩展后，原来那一对记录仍在资料里，且取值一字未改
    row_a = next(r for r in rows if r[0] == viol.left_a[0] and r[2] == viol.right_a)
    row_b = next(r for r in rows if r[0] == viol.left_b[0] and r[2] == viol.right_b)
    assert row_a[1] != row_b[1]  # 新列把它们区分开


def test_minimal_left_after_extension_is_part_not_supplier():
    columns, rows = VIOLATION_TABLE
    index = build_index(make_dataset(columns, rows))
    result = discover(index, 2, 2, "equal")
    for name in ("name",):
        lefts = {tuple(f.left) for f in result.fds if f.right == name}
        assert () not in lefts
        assert ("supplier",) not in lefts   # 供应商单列不成立
        assert ("part",) in lefts           # 零件号才是真正的最小左部
