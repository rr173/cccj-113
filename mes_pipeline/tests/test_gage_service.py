"""量具 R&R 服务端到端测试：演示场景、结果落盘、不变性核对、CLI。

不变性（交换零件/人员名字、打乱行序、整体平移、mm/μm 换算）均对
grr_demo/s1 数据施加变换后重算，统计结果必须不变（或按量纲比例变化）。
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from mes_pipeline.gage.service import (
    STATUS_INCOMPLETE,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_PARTIAL,
    load_result,
    run_study,
)

DEMO = Path(__file__).resolve().parent.parent / "grr_demo"
ROOT = Path(__file__).resolve().parent.parent
S1 = DEMO / "s1_interaction_kept"

STAT_SECTIONS = ("anova", "variance_components", "metrics")


def _run_s1(tmp_path: Path, calc_id: str = "t") -> dict:
    return run_study(S1 / "study.json", S1 / "observations.csv",
                     tmp_path, calc_id=calc_id)


def _write_variant(tmp_path: Path, name: str, study: dict, rows: list[list[str]]):
    """把变换后的试验定义与观测行写到 tmp_path/name/ 下。"""
    d = tmp_path / name
    d.mkdir(parents=True)
    study_path = d / "study.json"
    obs_path = d / "observations.csv"
    study_path.write_text(json.dumps(study, ensure_ascii=False), encoding="utf-8")
    lines = ["study_id,part_id,operator_id,repeat_id,value,unit"]
    lines += [",".join(r) for r in rows]
    obs_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return study_path, obs_path


def _s1_source() -> tuple[dict, list[list[str]]]:
    study = json.loads((S1 / "study.json").read_text(encoding="utf-8"))
    lines = (S1 / "observations.csv").read_text(encoding="utf-8").splitlines()
    rows = [line.split(",") for line in lines[1:]]
    return study, rows


def _assert_nested_close(a, b, rel=1e-9, abs_tol=1e-12, path=""):
    """递归断言两个嵌套结构的数值近似相等（用于平移/换算后的比对）。"""
    if isinstance(a, dict):
        assert isinstance(b, dict) and set(a) == set(b), f"{path}: 键不一致"
        for k in a:
            _assert_nested_close(a[k], b[k], rel, abs_tol, f"{path}.{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), f"{path}: 长度不一致"
        for i, (x, y) in enumerate(zip(a, b)):
            _assert_nested_close(x, y, rel, abs_tol, f"{path}[{i}]")
    elif isinstance(a, (int, float)) and not isinstance(a, bool):
        assert a == pytest.approx(b, rel=rel, abs=abs_tol), f"{path}: {a} != {b}"
    else:
        assert a == b, f"{path}: {a!r} != {b!r}"


# --------------------------------------------------------------------------- #
# 端到端：状态、落盘、读取
# --------------------------------------------------------------------------- #
def test_run_and_show_roundtrip(tmp_path):
    r = _run_s1(tmp_path)
    assert r["status"] == STATUS_OK
    loaded = load_result(tmp_path, "t")
    assert loaded == r  # 落盘内容与原结果一致
    assert loaded["anova"]["interaction_test"]["decision"] == "keep_interaction"


def test_failure_results_are_saved_and_loadable(tmp_path):
    d = DEMO / "s5_reject_then_fix"
    dup = run_study(d / "study.json", d / "observations_duplicate.csv",
                    tmp_path, calc_id="dup")
    assert dup["status"] == STATUS_INVALID
    assert load_result(tmp_path, "dup")["status"] == STATUS_INVALID
    assert any("重复行" in e for e in dup["errors"])

    miss = run_study(d / "study.json", d / "observations_missing_cell.csv",
                     tmp_path, calc_id="miss")
    assert miss["status"] == STATUS_INCOMPLETE
    assert any("缺格" in e for e in miss["errors"])

    fixed = run_study(d / "study.json", d / "observations_fixed.csv",
                      tmp_path, calc_id="fixed")
    assert fixed["status"] == STATUS_OK
    assert fixed["design"]["n_observations"] == 12


def test_partial_status_when_statistics_undefined(tmp_path):
    d = DEMO / "s4_all_equal"
    r = run_study(d / "study.json", d / "observations.csv", tmp_path, calc_id="s4")
    assert r["status"] == STATUS_PARTIAL
    # 不可定义的统计量明确列出原因，其余计算完整保存
    assert "undefined_statistics" in r
    assert any("ndc" in k for k in r["undefined_statistics"])
    assert r["anova"]["terms"]["total"]["ss"] == 0.0
    assert r["metrics"]["pct_tolerance_grr"] == 0.0
    assert r["anova"]["interaction_test"]["decision"] == "keep_interaction"


def test_invalid_alpha_rejected(tmp_path):
    r = run_study(S1 / "study.json", S1 / "observations.csv",
                  tmp_path, alpha=1.5, calc_id="bad-alpha")
    assert r["status"] == STATUS_INVALID
    assert any("alpha" in e for e in r["errors"])


def test_traceability_means_and_rows(tmp_path):
    r = _run_s1(tmp_path)
    means = r["means"]
    # 沿均方回溯：零件均值 → 行号 → 原始观测行
    p1 = next(x for x in means["by_part"] if x["part_id"] == "P1")
    assert p1["rows"] == [2, 3, 4, 5]
    assert p1["mean"] == pytest.approx(10.03)
    obs_by_row = {o["row"]: o for o in r["observations"]}
    vals = [obs_by_row[i]["canonical_value"] for i in p1["rows"]]
    assert sum(vals) / len(vals) == pytest.approx(p1["mean"])
    cell = next(x for x in means["cells"]
                if x["part_id"] == "P1" and x["operator_id"] == "OP-A")
    assert cell["rows"] == [2, 3]
    assert means["grand"]["mean"] == pytest.approx(10.01)


# --------------------------------------------------------------------------- #
# 不变性核对
# --------------------------------------------------------------------------- #
def test_invariance_renamed_parts_and_operators(tmp_path):
    base = _run_s1(tmp_path / "base")
    study, rows = _s1_source()
    ren = {"P1": "P3", "P3": "P1", "OP-A": "OP-B", "OP-B": "OP-A"}
    study["parts"] = [ren.get(p, p) for p in study["parts"]]
    study["operators"] = [ren.get(o, o) for o in study["operators"]]
    for r in rows:
        r[1] = ren.get(r[1], r[1])
        r[2] = ren.get(r[2], r[2])
    sp, op = _write_variant(tmp_path / "renamed", "v", study, rows)
    swapped = run_study(sp, op, tmp_path / "renamed" / "store")
    for section in STAT_SECTIONS:
        assert swapped[section] == base[section], f"{section} 应不随命名改变"


def test_invariance_row_order(tmp_path):
    base = _run_s1(tmp_path / "base")
    study, rows = _s1_source()
    random.Random(20261113).shuffle(rows)
    sp, op = _write_variant(tmp_path / "shuffled", "v", study, rows)
    shuffled = run_study(sp, op, tmp_path / "shuffled" / "store")
    # fsum 求和与顺序无关：统计段逐位相等
    for section in STAT_SECTIONS:
        assert shuffled[section] == base[section], f"{section} 应不随行序改变"
    # 均值数值也不变（行号定位随行号变化，属正常）
    assert [g["mean"] for g in shuffled["means"]["by_part"]] == \
           [g["mean"] for g in base["means"]["by_part"]]


def test_invariance_shift(tmp_path):
    base = _run_s1(tmp_path / "base")
    study, rows = _s1_source()
    study["spec"]["lower"] += 0.5
    study["spec"]["upper"] += 0.5
    for r in rows:
        r[4] = f"{float(r[4]) + 0.5:.2f}"
    sp, op = _write_variant(tmp_path / "shifted", "v", study, rows)
    shifted = run_study(sp, op, tmp_path / "shifted" / "store")
    for section in STAT_SECTIONS:
        _assert_nested_close(shifted[section], base[section], rel=1e-9)
    assert shifted["means"]["grand"]["mean"] == pytest.approx(
        base["means"]["grand"]["mean"] + 0.5)


def test_invariance_mm_vs_um(tmp_path):
    base = _run_s1(tmp_path / "base")
    study, rows = _s1_source()
    study["unit"] = "μm"
    study["spec"]["lower"] *= 1000.0
    study["spec"]["upper"] *= 1000.0
    for r in rows:
        r[4] = f"{float(r[4]) * 1000.0:g}"
        r[5] = "μm"
    sp, op = _write_variant(tmp_path / "um", "v", study, rows)
    um = run_study(sp, op, tmp_path / "um" / "store")

    # 方差类放大 1e6 倍，标准差类放大 1e3 倍，比例类完全一致
    for term in ("part", "operator", "interaction", "error", "total"):
        assert um["anova"]["terms"][term]["ss"] == pytest.approx(
            base["anova"]["terms"][term]["ss"] * 1e6, rel=1e-9)
    for name in ("repeatability", "reproducibility", "grr", "part", "total"):
        assert um["metrics"]["variance"][name] == pytest.approx(
            base["metrics"]["variance"][name] * 1e6, rel=1e-9)
        assert um["metrics"]["stddev"][name] == pytest.approx(
            base["metrics"]["stddev"][name] * 1e3, rel=1e-9)
    for section in ("contribution_pct", "pct_study_var"):
        _assert_nested_close(um["metrics"][section], base["metrics"][section],
                             rel=1e-9)
    assert um["metrics"]["pct_tolerance_grr"] == pytest.approx(
        base["metrics"]["pct_tolerance_grr"], rel=1e-9)
    assert um["metrics"]["ndc"] == base["metrics"]["ndc"]


def test_invariance_mixed_units_same_result(tmp_path):
    """同一试验内 mm 与 μm 行混写，结果与全 mm 一致。"""
    base = _run_s1(tmp_path / "base")
    study, rows = _s1_source()
    for i, r in enumerate(rows):
        if i % 2 == 0:  # 每隔一行改用 μm 书写
            r[4] = f"{float(r[4]) * 1000.0:g}"
            r[5] = "μm"
    sp, op = _write_variant(tmp_path / "mixed", "v", study, rows)
    mixed = run_study(sp, op, tmp_path / "mixed" / "store")
    for section in STAT_SECTIONS:
        _assert_nested_close(mixed[section], base[section], rel=1e-9)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-m", "mes_pipeline.cli", *args],
        cwd=ROOT, capture_output=True, text=True)


def test_cli_run_and_show(tmp_path):
    r = _cli("--store", str(tmp_path), "grr-run",
             "--study", str(S1 / "study.json"),
             "--observations", str(S1 / "observations.csv"),
             "--calc-id", "cli-s1")
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["status"] == "OK"

    s = _cli("--store", str(tmp_path), "grr-show", "--calc-id", "cli-s1")
    assert s.returncode == 0
    assert json.loads(s.stdout)["calc_id"] == "cli-s1"


def test_cli_exit_codes(tmp_path):
    d = DEMO / "s5_reject_then_fix"
    dup = _cli("--store", str(tmp_path), "grr-run",
               "--study", str(d / "study.json"),
               "--observations", str(d / "observations_duplicate.csv"),
               "--calc-id", "cli-dup")
    assert dup.returncode == 4  # 输入不合法

    miss = _cli("--store", str(tmp_path), "grr-run",
                "--study", str(d / "study.json"),
                "--observations", str(d / "observations_missing_cell.csv"),
                "--calc-id", "cli-miss")
    assert miss.returncode == 6  # 设计不完整

    s4 = _cli("--store", str(tmp_path), "grr-run",
              "--study", str(DEMO / "s4_all_equal" / "study.json"),
              "--observations", str(DEMO / "s4_all_equal" / "observations.csv"),
              "--calc-id", "cli-s4")
    assert s4.returncode == 0  # 个别统计量不可定义不影响退出码
    assert json.loads(s4.stdout)["status"] == "OK_PARTIAL"

    missing = _cli("--store", str(tmp_path), "grr-show", "--calc-id", "nope")
    assert missing.returncode == 2
