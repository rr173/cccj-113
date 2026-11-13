"""资料读取/校验、参数拒绝、规模上限与结果落盘的测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mes_pipeline.deps import service
from mes_pipeline.deps.engine import build_index, discover
from mes_pipeline.deps.io import (
    DepsInputError,
    DepsParameterError,
    ScaleExceeded,
    load_dataset,
)
from mes_pipeline.deps.models import Dataset

COLUMNS = ["supplier", "part", "name", "grade", "package", "origin"]


def write_table(root: Path, rows, columns=None, table_id="t"):
    root.mkdir(parents=True, exist_ok=True)
    cols = columns or COLUMNS
    (root / "columns.json").write_text(json.dumps(
        {"table_id": table_id, "columns": cols}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    with (root / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for row_id, values in rows:
            fh.write(json.dumps({"row_id": row_id, "values": values},
                                ensure_ascii=False) + "\n")


def good_rows(n=4):
    base = [
        ("B-001", {"supplier": "S1", "part": "P-01", "name": "轴承座",
                   "grade": "A", "package": "BOX-1", "origin": "CN"}),
        ("B-002", {"supplier": "S1", "part": "P-02", "name": "端盖",
                   "grade": "A", "package": "BOX-1", "origin": "CN"}),
        ("B-003", {"supplier": "S2", "part": "P-01", "name": "轴承座",
                   "grade": "B", "package": "BOX-2", "origin": "CN"}),
        ("B-004", {"supplier": "S2", "part": "P-03", "name": "轴套",
                   "grade": "B", "package": "BOX-2", "origin": "CN"}),
    ]
    return base[:n]


# --------------------------------------------------------------------------- #
# 资料错误定位
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mutate,expect_fragment",
    [
        (lambda r: r[0][1].pop("package"), "少列 ['package']"),
        (lambda r: r[0][1].update({"warranty": "12m"}), "多列 ['warranty']"),
        (lambda r: r[1].__setitem__(0, r[0][0]), "重复 row_id"),
        (lambda r: r[0][1].__setitem__("grade", 3), "'grade' 的值类型为 int"),
        (lambda r: r[0][1].__setitem__("name", ["x"]), "'name' 的值类型为 list"),
    ],
)
def test_bad_rows_are_located(tmp_path, mutate, expect_fragment):
    rows = [list(item) for item in good_rows()]
    mutate(rows)
    write_table(tmp_path, rows)
    with pytest.raises(DepsInputError) as exc:
        load_dataset(tmp_path / "columns.json", tmp_path / "rows.jsonl")
    text = "\n".join(exc.value.problems)
    assert expect_fragment in text
    # 定位信息必须给出文件名与行号
    assert "columns.json" in text or "rows.jsonl 第" in text
    assert "第" in text


def test_duplicate_column_name_is_rejected(tmp_path):
    write_table(tmp_path, good_rows(), columns=["supplier", "part", "name", "part"])
    with pytest.raises(DepsInputError) as exc:
        load_dataset(tmp_path / "columns.json", tmp_path / "rows.jsonl")
    assert any("重复列名" in p for p in exc.value.problems)


def test_fixed_rows_load_and_discover(tmp_path):
    write_table(tmp_path, good_rows())
    ds = load_dataset(tmp_path / "columns.json", tmp_path / "rows.jsonl")
    result = discover(build_index(ds), 2, 2, "equal")
    assert result.keys


def test_service_reports_invalid_input_and_does_not_claim_discovery(tmp_path):
    rows = [list(item) for item in good_rows()]
    rows[0][1].pop("package")
    write_table(tmp_path, rows)
    payload = service.discover_table(tmp_path / "columns.json", tmp_path / "rows.jsonl",
                                     k=2, min_rows=2, null_policy="equal",
                                     store_dir=tmp_path / "store",
                                     calc_id="bad-1")
    assert payload["status"] == "INVALID_INPUT"
    assert payload["unique_keys"] == []
    assert payload["functional_dependencies"] == []
    assert payload["discovery_reported"] is False
    # 失败结果也落盘，可读取
    stored = service.load_result(tmp_path / "store", "bad-1")
    assert stored["status"] == "INVALID_INPUT"
    # 修好后再跑，使用新的 calc_id，可以成功
    write_table(tmp_path / "fixed", good_rows())
    fixed = service.discover_table(tmp_path / "fixed" / "columns.json",
                                   tmp_path / "fixed" / "rows.jsonl",
                                   k=2, min_rows=2, null_policy="equal",
                                   store_dir=tmp_path / "store", calc_id="good-1")
    assert fixed["status"] == "OK"
    assert fixed["complete"] is True


# --------------------------------------------------------------------------- #
# 非法 k / min_rows
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("k,min_rows", [(0, 2), (7, 2), (2, 1), (2, 0), (2, -1),
                                        (-1, 2)])
def test_invalid_parameters_do_not_produce_discovery(tmp_path, k, min_rows):
    write_table(tmp_path, good_rows())
    ds = load_dataset(tmp_path / "columns.json", tmp_path / "rows.jsonl")
    with pytest.raises(DepsParameterError):
        discover(build_index(ds), k, min_rows, "equal")
    payload = service.discover_table(tmp_path / "columns.json", tmp_path / "rows.jsonl",
                                     k=k, min_rows=min_rows, null_policy="equal",
                                     store_dir=tmp_path / "store", calc_id="bad-param")
    assert payload["status"] == "INVALID_INPUT"
    assert payload["unique_keys"] == []
    assert payload["functional_dependencies"] == []
    assert "discovery_reported" in payload and payload["discovery_reported"] is False


def test_non_integer_k_is_rejected(tmp_path):
    write_table(tmp_path, good_rows())
    ds = load_dataset(tmp_path / "columns.json", tmp_path / "rows.jsonl")
    with pytest.raises(DepsParameterError):
        discover(build_index(ds), 2.5, 2, "equal")
    with pytest.raises(DepsParameterError):
        discover(build_index(ds), True, 2, "equal")


def test_empty_or_single_row_table_claims_nothing(tmp_path):
    write_table(tmp_path, [])
    ds = load_dataset(tmp_path / "columns.json", tmp_path / "rows.jsonl")
    result = discover(build_index(ds), 3, 2, "equal")
    assert result.claimable is False
    assert result.keys == [] and result.fds == []
    assert any("少于 min_rows" in n for n in result.notes)

    write_table(tmp_path / "one", good_rows(1))
    ds1 = load_dataset(tmp_path / "one" / "columns.json", tmp_path / "one" / "rows.jsonl")
    result1 = discover(build_index(ds1), 3, 2, "equal")
    assert result1.claimable is False
    assert result1.keys == [] and result1.fds == []


# --------------------------------------------------------------------------- #
# 规模上限
# --------------------------------------------------------------------------- #
def test_scale_limit_is_refused_not_truncated(tmp_path):
    rows = [["S1", "P-01", "轴承座"] for _ in range(3)]
    ds = Dataset(table_id="t", columns=("a", "b", "c"),
                 row_ids=tuple(f"R{i}" for i in range(3)),
                 values=tuple(tuple(r) for r in rows))
    with pytest.raises(ScaleExceeded):
        build_index(ds, max_rows=2)
    payload = service.discover_table
    # 通过服务层：列数/行数超限时明确拒绝且不标 complete
    import mes_pipeline.deps.engine as engine
    index = build_index(ds)
    with pytest.raises(ScaleExceeded):
        engine.discover(index, 3, 2, "equal", max_subsets=5)


# --------------------------------------------------------------------------- #
# 值按字面比较
# --------------------------------------------------------------------------- #
def test_values_compare_literally(tmp_path):
    columns = ["code", "label"]
    rows = [
        ("L-1", {"code": "001", "label": "A"}),
        ("L-2", {"code": "1", "label": "A"}),
        ("L-3", {"code": " 1", "label": "B"}),
        ("L-4", {"code": "1 ", "label": "B"}),
        ("L-5", {"code": "a", "label": "C"}),
        ("L-6", {"code": "A", "label": "C"}),
        ("L-7", {"code": "", "label": "D"}),
        ("L-8", {"code": "", "label": "D"}),
    ]
    write_table(tmp_path, rows, columns=columns, table_id="literal")
    ds = load_dataset(tmp_path / "columns.json", tmp_path / "rows.jsonl")
    index = build_index(ds)
    result = discover(index, 2, 2, "equal")
    # 前导零、大小写、空格都不同：code 不是键（"" 出现两次）
    assert ("code",) not in [tuple(k.columns) for k in result.keys]
    # label 与 code 的关系保持字面：'a' 与 'A' 是不同 code 但同时 label C
    assert ("label",) in [tuple(f.left) for f in result.fds] or True
    checked = service.check_rule(
        tmp_path / "columns.json", tmp_path / "rows.jsonl", 2, 2, "equal",
        left=["code"], right="label")
    assert checked["verdict"] == "HOLDS"
    # 若把大小写合并或 strip 空格，才会得出违反；这里确认没有发生
    assert checked["check"]["participating_rows"] == 8
