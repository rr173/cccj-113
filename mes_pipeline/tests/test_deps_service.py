"""端到端测试：CLI、结果落盘/读取、打乱顺序不变性、规模样本的一致性。

这些测试直接构造临时表并调用命令行入口，确保仓库同事按 README 命令即可运行。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from mes_pipeline.cli import main as cli_main
from mes_pipeline.deps import service
from mes_pipeline.deps.bruteforce import discover_all
from mes_pipeline.deps.engine import build_index, discover
from mes_pipeline.deps.io import load_dataset
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


def dataset_rows(n=40, seed=0):
    rng = random.Random(seed)
    suppliers = [f"S{n:02d}" for n in range(6)]
    parts = [f"P-{n:02d}" for n in range(8)]
    names = ["轴承座", "端盖", "轴套", "垫圈"]
    grades = ["A", "B", "C"]
    packages = [f"BOX-{n}" for n in range(4)]
    rows = []
    for i in range(n):
        supplier = rng.choice(suppliers)
        part = rng.choice(parts)
        rows.append((f"R{i:04d}", {
            "supplier": supplier,
            "part": part,
            "name": names[int(part[-2:]) % len(names)],
            "grade": grades[suppliers.index(supplier) % len(grades)],
            "package": packages[suppliers.index(supplier) % len(packages)],
            "origin": "CN",
        }))
    return rows


def run_cli(argv, capsys):
    code = cli_main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


# --------------------------------------------------------------------------- #
# 顺序无关
# --------------------------------------------------------------------------- #
def test_shuffled_rows_give_identical_results_and_counterexamples(tmp_path):
    rows = dataset_rows(40, seed=3)
    write_table(tmp_path / "a", rows)
    shuffled = list(rows)
    random.Random(99).shuffle(shuffled)
    write_table(tmp_path / "b", shuffled)

    results = {}
    for name in ("a", "b"):
        payload = service.discover_table(
            tmp_path / name / "columns.json", tmp_path / name / "rows.jsonl",
            k=3, min_rows=2, null_policy="equal",
            store_dir=tmp_path / "store", calc_id=f"shuffle-{name}")
        results[name] = payload
    # 发现集合（列组合、顺序、证据计数）完全一致
    strip = lambda d: json.dumps(
        {"keys": d["unique_keys"], "fds": d["functional_dependencies"],
         "gaps": d["evidence_gaps"], "stats": d["stats"]},
        ensure_ascii=False, sort_keys=True)
    assert strip(results["a"]) == strip(results["b"])

    # 反例也完全一致（包括 row_id 对与字段值）
    for rule in [("supplier", "name"), ("part", "package"), ("grade", "supplier")]:
        checks = []
        for name in ("a", "b"):
            checks.append(service.check_rule(
                tmp_path / name / "columns.json", tmp_path / name / "rows.jsonl",
                k=3, min_rows=2, null_policy="equal",
                left=[rule[0]], right=rule[1]))
        assert checks[0]["verdict"] == checks[1]["verdict"]
        assert checks[0]["counterexample"] == checks[1]["counterexample"]


# --------------------------------------------------------------------------- #
# CLI 端到端
# --------------------------------------------------------------------------- #
def test_cli_discover_check_and_show(tmp_path, capsys):
    rows = dataset_rows(30, seed=5)
    write_table(tmp_path / "tbl", rows)
    store = tmp_path / "store"
    common = ["--columns", str(tmp_path / "tbl/columns.json"),
              "--rows", str(tmp_path / "tbl/rows.jsonl"), "--k", "3",
              "--min-rows", "2", "--null-policy", "equal"]

    code, payload = run_cli(["--store", str(store), "deps-discover", *common,
                             "--calc-id", "e2e"], capsys)
    assert code == 0
    assert payload["status"] == "OK" and payload["complete"] is True
    assert payload["claimable"] is True
    assert payload["unique_keys"] or payload["functional_dependencies"]

    # 结果已落盘，可按 ID 读取
    code, shown = run_cli(["--store", str(store), "deps-show",
                           "--calc-id", "e2e"], capsys)
    assert code == 0
    assert shown["unique_keys"] == payload["unique_keys"]

    # 核查一条违反的依赖：反例带 row_id 与字段值
    code, checked = run_cli(["--store", str(store), "deps-check", *common,
                             "--left", "supplier", "--right", "name"], capsys)
    assert code == 0
    assert checked["verdict"] in ("HOLDS", "VIOLATED", "INSUFFICIENT_EVIDENCE")
    if checked["verdict"] == "VIOLATED":
        cex = checked["counterexample"]
        assert cex["row_id_a"] < cex["row_id_b"]
        assert cex["right_value_a"] != cex["right_value_b"]
        assert cex["left_values_a"] == cex["left_values_b"]

    # 键核查
    code, checked_key = run_cli(["--store", str(store), "deps-check", *common,
                                 "--key", "supplier", "--key", "part"], capsys)
    assert code == 0
    assert checked_key["rule"]["kind"] == "unique_key"


def test_cli_bad_parameters_and_bad_rows_have_no_fake_success(tmp_path, capsys):
    write_table(tmp_path / "tbl", dataset_rows(5))
    store = str(tmp_path / "store")
    base = ["--columns", str(tmp_path / "tbl/columns.json"),
            "--rows", str(tmp_path / "tbl/rows.jsonl")]

    code, payload = run_cli(["--store", store, "deps-discover", *base,
                             "--k", "9", "--min-rows", "2"], capsys)
    assert code == 4
    assert payload["status"] == "INVALID_INPUT"
    assert payload["unique_keys"] == [] and payload["functional_dependencies"] == []
    assert payload["discovery_reported"] is False

    code, payload = run_cli(["--store", store, "deps-discover", *base,
                             "--k", "2", "--min-rows", "1"], capsys)
    assert code == 4
    assert payload["unique_keys"] == []

    # 坏行：定位到行号与列名，退出码 4
    write_table(tmp_path / "bad", [("B-1", {"supplier": "S1", "part": "P-1"})])
    code, payload = run_cli(["--store", str(tmp_path / "store2"),
                             "deps-discover",
                             "--columns", str(tmp_path / "bad/columns.json"),
                             "--rows", str(tmp_path / "bad/rows.jsonl"),
                             "--k", "2", "--min-rows", "2"], capsys)
    assert code == 4
    assert payload["status"] == "INVALID_INPUT"
    assert any("少列" in err for err in payload["errors"])


def test_cli_explain_returns_minimal_lefts(tmp_path, capsys):
    rows = [
        ("V-001", {"supplier": "S1", "part": "P-01", "name": "轴承座",
                   "grade": "A", "package": "BOX-1", "origin": "CN"}),
        ("V-002", {"supplier": "S1", "part": "P-02", "name": "端盖",
                   "grade": "A", "package": "BOX-1", "origin": "CN"}),
        ("V-003", {"supplier": "S2", "part": "P-01", "name": "轴承座",
                   "grade": "B", "package": "BOX-2", "origin": "CN"}),
        ("V-004", {"supplier": "S3", "part": "P-03", "name": "轴套",
                   "grade": "C", "package": "BOX-3", "origin": "CN"}),
    ]
    write_table(tmp_path / "v", rows)
    common = ["--columns", str(tmp_path / "v/columns.json"),
              "--rows", str(tmp_path / "v/rows.jsonl"),
              "--k", "3", "--min-rows", "2", "--null-policy", "equal"]
    code, payload = run_cli(["--store", str(tmp_path / "store"), "deps-explain",
                             *common, "--left", "supplier", "--right", "name"],
                            capsys)
    assert code == 0
    assert payload["status"] == "OK"
    assert payload["repaired_by_values"] is False
    assert payload["minimal_lefts_for_right"]
    # 加 part 后成立
    ext = {e["added_column"]: e for e in payload["extensions"]}
    assert ext["part"]["verdict"] == "HOLDS"


# --------------------------------------------------------------------------- #
# 规模样本上的结果与独立枚举器一致（小规模影子表）
# --------------------------------------------------------------------------- #
def test_synthetic_generator_and_bruteforce_agree_on_the_demo_tables(tmp_path):
    from synthetic import deps_generate

    for name, builder in deps_generate.SCENARIOS["demo"]:
        root = tmp_path / name
        builder(root)
        try:
            ds = load_dataset(root / "columns.json", root / "rows.jsonl")
        except Exception:
            continue  # 故意不法的资料（s5/s7）另行测试
        index = build_index(ds)
        for policy in ("equal", "ignore"):
            result = discover(index, 3, 2, policy).as_dict()
            brute = discover_all(ds.columns, ds.row_ids, ds.values, 3, 2, policy)
            assert sorted(tuple(x["columns"]) for x in result["unique_keys"]) == \
                sorted(tuple(x["columns"]) for x in brute["unique_keys"])
            assert sorted((tuple(x["left"]), x["right"])
                          for x in result["functional_dependencies"]) == \
                sorted((tuple(x["left"]), x["right"])
                       for x in brute["functional_dependencies"])
