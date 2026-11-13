"""配料服务端到端测试：演示场景、保守口径、结果落盘、CLI。

已知最优解均经手工核算；整桶场景另以穷举核对。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mes_pipeline.charge.io import load_charge_input
from mes_pipeline.charge.models import (
    STATUS_INFEASIBLE,
    STATUS_INVALID,
    STATUS_OPTIMAL,
    AssayInterval,
    ChargeInput,
)
from mes_pipeline.charge.service import build_spec, load_result, solve_charge
from mes_pipeline.charge.solver import MILP_INFEASIBLE, MILP_OPTIMAL, solve_milp

DEMO = Path(__file__).resolve().parent.parent / "charge_demo"
ROOT = Path(__file__).resolve().parent.parent


def _solve(tmp_path: Path, scenario: str, calc_id: str = "t",
           assays_name: str = "assays.csv") -> dict:
    d = DEMO / scenario
    return solve_charge(d / "inventory.csv", d / assays_name, d / "certificates.csv",
                        d / "heat.json", tmp_path, calc_id=calc_id)


# --------------------------------------------------------------------------- #
# 演示场景：已知最优解
# --------------------------------------------------------------------------- #
def test_s1_bulk_known_optimum(tmp_path):
    r = _solve(tmp_path, "s1_bulk_optimum")
    assert r["status"] == STATUS_OPTIMAL
    plan = r["plan"]
    assert plan["total_cost"] == pytest.approx(5000.0)
    kg = {l["lot_id"]: l["kg"] for l in plan["lines"]}
    assert kg == {"LOT-FE-A": pytest.approx(500.0), "LOT-FE-B": pytest.approx(500.0)}
    cr = next(e for e in plan["elements"] if e["element"] == "Cr")
    assert cr["worst_min"] == pytest.approx(18.0)
    assert cr["margin_min"] == pytest.approx(0.0)
    # 使用过的化验证书
    certs = {(c["lot_id"], c["element"], c["cert_id"]) for c in plan["certificates_used"]}
    assert ("LOT-FE-A", "Cr", "CERT-A-01") in certs
    # 未用库存
    unused = {u["lot_id"]: u for u in plan["unused_inventory"]}
    assert unused["LOT-FE-C"]["remaining_usable_kg"] == pytest.approx(400.0)


def test_s2_drum_relaxation_cannot_be_poured(tmp_path):
    r = _solve(tmp_path, "s2_drum_relaxation")
    assert r["status"] == STATUS_OPTIMAL
    plan = r["plan"]
    # 连续松弛 500 < 整数最优 550，且松弛解含 2.5 桶（100kg/40kg）无法落地
    assert r["solver"]["continuous_relaxation_cost"] == pytest.approx(500.0)
    assert plan["total_cost"] == pytest.approx(550.0)
    lines = {l["lot_id"]: l for l in plan["lines"]}
    assert lines["LOT-DRUM-1"]["drums"] == 3
    assert lines["LOT-DRUM-1"]["kg"] == pytest.approx(120.0)
    assert lines["LOT-DRUM-2"]["drums"] == 3
    assert lines["LOT-BULK-1"]["kg"] == pytest.approx(5.0)
    # 不足一桶的余量不得使用
    unused = {u["lot_id"]: u for u in plan["unused_inventory"]}
    assert unused["LOT-DRUM-1"]["unusable_remainder_kg"] == pytest.approx(15.0)
    assert unused["LOT-DRUM-2"]["unusable_remainder_kg"] == pytest.approx(15.0)


def test_s2_exhaustive_enumeration_matches(tmp_path):
    """小型整桶场景：穷举全部桶数组合核对最优费用。"""
    r = _solve(tmp_path, "s2_drum_relaxation")
    assert r["status"] == STATUS_OPTIMAL
    best = None
    # LOT-DRUM-1 可用 5 桶(40kg)，LOT-DRUM-2 可用 7 桶(25kg)，散装补足
    for n1 in range(6):
        for n2 in range(8):
            bulk = 200 - 40 * n1 - 25 * n2
            if not (0 <= bulk <= 300):
                continue
            if 0.18 * 40 * n1 + 0.10 * 25 * n2 + 0.30 * bulk < 0.14 * 200 - 1e-9:
                continue
            cost = 3.0 * 40 * n1 + 2.0 * 25 * n2 + 8.0 * bulk
            best = cost if best is None else min(best, cost)
    assert best == pytest.approx(550.0)
    assert r["plan"]["total_cost"] == pytest.approx(best)


def test_s3_detection_limit_conservative_infeasible(tmp_path):
    r = _solve(tmp_path, "s3_detection_limit")
    assert r["status"] == STATUS_INFEASIBLE
    cids = {c["cid"] for c in r["conflict"]["constraints"]}
    assert cids == {"mass_balance", "element_max:Pb", "availability:LOT-PB-B"}


def test_s3_midpoint_would_misjudge(tmp_path):
    """同一输入把区间收缩为中点后“可行”，证明中点法误判；保守口径判不可行。"""
    d = DEMO / "s3_detection_limit"
    charge = load_charge_input(d / "inventory.csv", d / "assays.csv",
                               d / "certificates.csv", d / "heat.json")
    spec, _ = build_spec(charge)
    assert solve_milp(spec).status == MILP_INFEASIBLE
    collapsed = tuple(
        AssayInterval(a.lot_id, a.element, (a.lo + a.hi) / 2, (a.lo + a.hi) / 2, a.cert_ids)
        for a in charge.assays
    )
    mid_spec, _ = build_spec(ChargeInput(charge.lots, collapsed, charge.heat))
    assert solve_milp(mid_spec).status == MILP_OPTIMAL  # 误判为可行


def test_s3_fixed_resolves(tmp_path):
    r = _solve(tmp_path, "s3_fixed")
    assert r["status"] == STATUS_OPTIMAL
    assert r["plan"]["total_cost"] == pytest.approx(200.0)


def test_s4_mutex_respected(tmp_path):
    r = _solve(tmp_path, "s4_mutex")
    assert r["status"] == STATUS_OPTIMAL
    plan = r["plan"]
    kg = {l["lot_id"]: l["kg"] for l in plan["lines"]}
    assert set(kg) == {"LOT-MX-2"}  # 互斥对不得同时入炉
    assert plan["total_cost"] == pytest.approx(350.0)
    # 无互斥的连续松弛 325 会选择被禁止的组合
    assert r["solver"]["continuous_relaxation_cost"] == pytest.approx(325.0)


def test_s5_shortage_iis(tmp_path):
    r = _solve(tmp_path, "s5_shortage")
    assert r["status"] == STATUS_INFEASIBLE
    cids = {c["cid"] for c in r["conflict"]["constraints"]}
    assert cids == {"mass_balance", "availability:LOT-S-1",
                    "availability:LOT-S-2", "availability:LOT-S-3"}
    # 冲突集可重新求解验证：整体不可行，移除任意一条恢复可行
    d = DEMO / "s5_shortage"
    charge = load_charge_input(d / "inventory.csv", d / "assays.csv",
                               d / "certificates.csv", d / "heat.json")
    spec, _ = build_spec(charge)
    assert solve_milp(spec, cids).status == MILP_INFEASIBLE
    for cid in cids:
        assert solve_milp(spec, cids - {cid}).status == MILP_OPTIMAL


def test_s6_mixed_units(tmp_path):
    r = _solve(tmp_path, "s6_mixed_units")
    assert r["status"] == STATUS_OPTIMAL
    assert r["plan"]["total_cost"] == pytest.approx(1400.0)
    kg = {l["lot_id"]: l["kg"] for l in r["plan"]["lines"]}
    assert kg["LOT-U-1"] == pytest.approx(200.0)
    assert kg["LOT-U-2"] == pytest.approx(100.0)  # Cr 以 ppm 化验，与 % 要求统一核算


# --------------------------------------------------------------------------- #
# 非法输入与状态区分
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("variant,needle", [
    ("missing_cert", "未登记"),
    ("conflict", "相互冲突"),
    ("negative", "负数"),
    ("unknown_unit", "单位不明"),
    ("missing_element", "不按零填充"),
])
def test_s7_invalid_variants_rejected(tmp_path, variant, needle):
    r = _solve(tmp_path, "s7_invalid_input", calc_id=f"bad-{variant}",
               assays_name=f"assays_{variant}.csv")
    assert r["status"] == STATUS_INVALID
    assert any(needle in e for e in r["errors"])
    assert "plan" not in r  # 不合法输入绝不出方案


def test_s7_corrected_input_solves(tmp_path):
    r = _solve(tmp_path, "s7_invalid_input", calc_id="ok", assays_name="assays_ok.csv")
    assert r["status"] == STATUS_OPTIMAL
    assert r["plan"]["total_cost"] == pytest.approx(250.0)


def test_invalid_result_is_persisted_and_readable(tmp_path):
    r = _solve(tmp_path, "s7_invalid_input", calc_id="bad", assays_name="assays_negative.csv")
    again = load_result(tmp_path, "bad")
    assert again["status"] == STATUS_INVALID
    assert again["errors"] == r["errors"]


# --------------------------------------------------------------------------- #
# 结果落盘与读取
# --------------------------------------------------------------------------- #
def test_result_roundtrip(tmp_path):
    r = _solve(tmp_path, "s1_bulk_optimum", calc_id="rt")
    loaded = load_result(tmp_path, "rt")
    assert loaded == r
    with pytest.raises(KeyError, match="未找到计算结果"):
        load_result(tmp_path, "no-such-calc")


# --------------------------------------------------------------------------- #
# 多个最优方案的等价性说明
# --------------------------------------------------------------------------- #
def test_alternate_optima_reported(tmp_path):
    # 两批完全同价同成分的原料：最优费用下投料量可互换
    inv = tmp_path / "inventory.csv"
    inv.write_text("lot_id,material,mode,available_kg,price_per_kg,drum_net_kg\n"
                   "L1,m,bulk,100,5.0,\nL2,m,bulk,100,5.0,\n", encoding="utf-8")
    certs = tmp_path / "certificates.csv"
    certs.write_text("cert_id,lab,issued_at\nC1,lab,2026-11-01\n", encoding="utf-8")
    assays = tmp_path / "assays.csv"
    assays.write_text("lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                      "L1,Cr,C1,pct,single,18.0,,\nL2,Cr,C1,pct,single,18.0,,\n",
                      encoding="utf-8")
    heat = tmp_path / "heat.json"
    heat.write_text(json.dumps({"heat_id": "H-ALT", "total_mass_kg": 100,
                                "elements": {"Cr": {"min_pct": 17.0}}, "mutex": []}),
                    encoding="utf-8")
    r = solve_charge(inv, assays, certs, heat, tmp_path, calc_id="alt")
    assert r["status"] == STATUS_OPTIMAL
    assert r["plan"]["total_cost"] == pytest.approx(500.0)
    alt = r["plan"]["alternate_optima"]
    assert alt["exists"] is True
    ranges = {x["lot_id"]: x for x in alt["kg_ranges"]}
    assert ranges["L1"]["kg_min"] == pytest.approx(0.0)
    assert ranges["L1"]["kg_max"] == pytest.approx(100.0)


def test_unique_optimum_reported(tmp_path):
    r = _solve(tmp_path, "s2_drum_relaxation")
    assert r["plan"]["alternate_optima"]["exists"] is False


# --------------------------------------------------------------------------- #
# CLI 端到端
# --------------------------------------------------------------------------- #
def _cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-m", "mes_pipeline.cli", *argv],
        capture_output=True, text=True, cwd=ROOT,
    )


def test_cli_solve_and_show(tmp_path):
    d = DEMO / "s1_bulk_optimum"
    proc = _cli("--store", str(tmp_path), "charge-solve",
                "--inventory", str(d / "inventory.csv"),
                "--assays", str(d / "assays.csv"),
                "--certs", str(d / "certificates.csv"),
                "--heat", str(d / "heat.json"),
                "--calc-id", "cli-1")
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == STATUS_OPTIMAL
    assert result["plan"]["total_cost"] == pytest.approx(5000.0)

    show = _cli("--store", str(tmp_path), "charge-show", "--calc-id", "cli-1")
    assert show.returncode == 0
    assert json.loads(show.stdout)["calc_id"] == "cli-1"


def test_cli_exit_codes(tmp_path):
    d = DEMO / "s5_shortage"
    proc = _cli("--store", str(tmp_path), "charge-solve",
                "--inventory", str(d / "inventory.csv"),
                "--assays", str(d / "assays.csv"),
                "--certs", str(d / "certificates.csv"),
                "--heat", str(d / "heat.json"),
                "--calc-id", "cli-infeasible")
    assert proc.returncode == 3  # 约束无解
    d = DEMO / "s7_invalid_input"
    proc = _cli("--store", str(tmp_path), "charge-solve",
                "--inventory", str(d / "inventory.csv"),
                "--assays", str(d / "assays_negative.csv"),
                "--certs", str(d / "certificates.csv"),
                "--heat", str(d / "heat.json"),
                "--calc-id", "cli-invalid")
    assert proc.returncode == 4  # 输入不合法
    show = _cli("--store", str(tmp_path), "charge-show", "--calc-id", "missing")
    assert show.returncode == 2
