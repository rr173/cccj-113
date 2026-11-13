"""配料输入校验测试：数据问题必须明确拒绝，缺值不按零填充。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mes_pipeline.charge.io import ChargeInputError, load_charge_input
from mes_pipeline.charge.models import MODE_BULK, MODE_DRUM

DEMO = Path(__file__).resolve().parent.parent / "charge_demo"
S1 = DEMO / "s1_bulk_optimum"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _base_files(tmp_path: Path):
    inv = _write(tmp_path, "inventory.csv",
                 "lot_id,material,mode,available_kg,price_per_kg,drum_net_kg\n"
                 "L1,m,bulk,100,5.0,\n"
                 "L2,m,bulk,100,6.0,\n")
    certs = _write(tmp_path, "certificates.csv", "cert_id,lab,issued_at\nC1,lab,2026-11-01\n")
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,pct,single,18.0,,\n"
                    "L2,Cr,C1,pct,single,20.0,,\n")
    heat = _write(tmp_path, "heat.json",
                  json.dumps({"heat_id": "H", "total_mass_kg": 50,
                              "elements": {"Cr": {"min_pct": 17.0}}, "mutex": []}))
    return inv, assays, certs, heat


def test_valid_input_loads(tmp_path):
    inv, assays, certs, heat = _base_files(tmp_path)
    charge = load_charge_input(inv, assays, certs, heat)
    assert len(charge.lots) == 2
    amap = charge.assay_map()
    assert amap[("L1", "Cr")].lo == pytest.approx(0.18)
    assert amap[("L2", "Cr")].hi == pytest.approx(0.20)


def test_s1_demo_loads_with_ppm_and_pct():
    charge = load_charge_input(S1 / "inventory.csv", S1 / "assays.csv",
                               S1 / "certificates.csv", S1 / "heat.json")
    amap = charge.assay_map()
    assert amap[("LOT-FE-A", "Cr")].lo == pytest.approx(0.20)
    assert amap[("LOT-FE-A", "Pb")].hi == pytest.approx(10e-6)  # ppm 换算为质量分数


def test_limit_value_is_closed_interval_not_midpoint(tmp_path):
    inv, _, certs, heat = _base_files(tmp_path)
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,ppm,limit,,,20\n"
                    "L2,Cr,C1,ppm,single,5,,\n")
    charge = load_charge_input(inv, assays, certs, heat)
    a = charge.assay_map()[("L1", "Cr")]
    assert a.lo == 0.0
    assert a.hi == pytest.approx(20e-6)  # [0, 20ppm]，不取中点 10ppm


def test_overlapping_results_intersect(tmp_path):
    inv, _, certs, heat = _base_files(tmp_path)
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,pct,range,,17.0,19.0\n"
                    "L1,Cr,C1,pct,range,,18.0,20.0\n"
                    "L2,Cr,C1,pct,single,20.0,,\n")
    charge = load_charge_input(inv, assays, certs, heat)
    a = charge.assay_map()[("L1", "Cr")]
    assert a.lo == pytest.approx(0.18)
    assert a.hi == pytest.approx(0.19)


def test_conflicting_results_rejected(tmp_path):
    inv, _, certs, heat = _base_files(tmp_path)
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,pct,range,,17.0,19.0\n"
                    "L1,Cr,C1,pct,range,,20.0,22.0\n"
                    "L2,Cr,C1,pct,single,20.0,,\n")
    with pytest.raises(ChargeInputError, match="相互冲突"):
        load_charge_input(inv, assays, certs, heat)


def test_missing_certificate_rejected(tmp_path):
    inv, _, certs, heat = _base_files(tmp_path)
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,CERT-GHOST,pct,single,18.0,,\n"
                    "L2,Cr,C1,pct,single,20.0,,\n")
    with pytest.raises(ChargeInputError, match="未登记"):
        load_charge_input(inv, assays, certs, heat)


def test_empty_certificate_rejected(tmp_path):
    inv, _, certs, heat = _base_files(tmp_path)
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,,pct,single,18.0,,\n"
                    "L2,Cr,C1,pct,single,20.0,,\n")
    with pytest.raises(ChargeInputError, match="缺证书号"):
        load_charge_input(inv, assays, certs, heat)


def test_negative_value_rejected(tmp_path):
    inv, _, certs, heat = _base_files(tmp_path)
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,pct,single,-0.5,,\n"
                    "L2,Cr,C1,pct,single,20.0,,\n")
    with pytest.raises(ChargeInputError, match="负数"):
        load_charge_input(inv, assays, certs, heat)


def test_unknown_unit_rejected(tmp_path):
    inv, _, certs, heat = _base_files(tmp_path)
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,ppb,single,18.0,,\n"
                    "L2,Cr,C1,pct,single,20.0,,\n")
    with pytest.raises(ChargeInputError, match="单位不明"):
        load_charge_input(inv, assays, certs, heat)


def test_missing_assay_not_zero_filled(tmp_path):
    inv, _, certs, heat = _base_files(tmp_path)
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,pct,single,18.0,,\n")
    with pytest.raises(ChargeInputError, match="不按零填充"):
        load_charge_input(inv, assays, certs, heat)


def test_assay_for_unknown_lot_rejected(tmp_path):
    inv, _, certs, heat = _base_files(tmp_path)
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,pct,single,18.0,,\n"
                    "L2,Cr,C1,pct,single,20.0,,\n"
                    "L9,Cr,C1,pct,single,20.0,,\n")
    with pytest.raises(ChargeInputError, match="不在库存表"):
        load_charge_input(inv, assays, certs, heat)


def test_drum_requires_net_weight(tmp_path):
    _, assays, certs, heat = _base_files(tmp_path)
    inv = _write(tmp_path, "inventory.csv",
                 "lot_id,material,mode,available_kg,price_per_kg,drum_net_kg\n"
                 "L1,m,drum,100,5.0,\n")
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,pct,single,18.0,,\n")
    with pytest.raises(ChargeInputError, match="drum_net_kg"):
        load_charge_input(inv, assays, certs, heat)


def test_bulk_must_not_have_net_weight(tmp_path):
    _, _, certs, heat = _base_files(tmp_path)
    inv = _write(tmp_path, "inventory.csv",
                 "lot_id,material,mode,available_kg,price_per_kg,drum_net_kg\n"
                 "L1,m,bulk,100,5.0,25\n"
                 "L2,m,bulk,100,6.0,\n")
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,pct,single,18.0,,\n"
                    "L2,Cr,C1,pct,single,20.0,,\n")
    with pytest.raises(ChargeInputError, match="不应填写"):
        load_charge_input(inv, assays, certs, heat)


def test_duplicate_lot_rejected(tmp_path):
    _, assays, certs, heat = _base_files(tmp_path)
    inv = _write(tmp_path, "inventory.csv",
                 "lot_id,material,mode,available_kg,price_per_kg,drum_net_kg\n"
                 "L1,m,bulk,100,5.0,\n"
                 "L1,m,bulk,100,6.0,\n")
    with pytest.raises(ChargeInputError, match="重复"):
        load_charge_input(inv, assays, certs, heat)


def test_heat_min_greater_than_max_rejected(tmp_path):
    inv, assays, certs, _ = _base_files(tmp_path)
    heat = _write(tmp_path, "heat.json",
                  json.dumps({"heat_id": "H", "total_mass_kg": 50,
                              "elements": {"Cr": {"min_pct": 19.0, "max_pct": 18.0}}}))
    with pytest.raises(ChargeInputError, match="下限大于上限"):
        load_charge_input(inv, assays, certs, heat)


def test_mutex_unknown_lot_rejected(tmp_path):
    inv, assays, certs, _ = _base_files(tmp_path)
    heat = _write(tmp_path, "heat.json",
                  json.dumps({"heat_id": "H", "total_mass_kg": 50,
                              "elements": {"Cr": {"min_pct": 17.0}},
                              "mutex": [["L1", "L9"]]}))
    with pytest.raises(ChargeInputError, match="互斥对引用的批次"):
        load_charge_input(inv, assays, certs, heat)


def test_heat_top_level_must_be_object(tmp_path):
    inv, assays, certs, _ = _base_files(tmp_path)
    heat = _write(tmp_path, "heat.json", json.dumps([1, 2, 3]))
    with pytest.raises(ChargeInputError, match="顶层必须是对象"):
        load_charge_input(inv, assays, certs, heat)


def test_heat_mutex_must_be_list(tmp_path):
    inv, assays, certs, _ = _base_files(tmp_path)
    heat = _write(tmp_path, "heat.json",
                  json.dumps({"heat_id": "H", "total_mass_kg": 50,
                              "elements": {"Cr": {"min_pct": 17.0}},
                              "mutex": {"a": 1}}))
    with pytest.raises(ChargeInputError, match="mutex 必须是数组"):
        load_charge_input(inv, assays, certs, heat)


def test_drum_usable_rounds_down_to_full_drums(tmp_path):
    _, _, certs, heat = _base_files(tmp_path)
    inv = _write(tmp_path, "inventory.csv",
                 "lot_id,material,mode,available_kg,price_per_kg,drum_net_kg\n"
                 "L1,m,drum,215,5.0,40\n"
                 "L2,m,bulk,100,6.0,\n")
    assays = _write(tmp_path, "assays.csv",
                    "lot_id,element,cert_id,unit,value_type,value,lower,upper\n"
                    "L1,Cr,C1,pct,single,18.0,,\n"
                    "L2,Cr,C1,pct,single,20.0,,\n")
    charge = load_charge_input(inv, assays, certs, heat)
    lot = {l.lot_id: l for l in charge.lots}["L1"]
    assert lot.mode == MODE_DRUM
    assert lot.usable_kg == pytest.approx(200.0)  # 5 桶，余 15 kg 不可用
    assert lot.unusable_remainder_kg == pytest.approx(15.0)
    assert {l.lot_id: l for l in charge.lots}["L2"].mode == MODE_BULK
