"""量具 R&R 输入读取与校验测试：合法解析 + 全部拒绝路径。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mes_pipeline.gage.io import (
    GageDesignError,
    GageInputError,
    load_gage_input,
    load_study,
)
from mes_pipeline.gage.models import UNIT_MM, UNIT_UM, convert_length, normalize_unit

DEMO = Path(__file__).resolve().parent.parent / "grr_demo"


def _write_study(tmp_path: Path, **overrides) -> Path:
    spec = {
        "study_id": "T",
        "characteristic": "轴径",
        "unit": "mm",
        "parts": ["P1", "P2", "P3"],
        "operators": ["OP-A", "OP-B"],
        "repeats": 2,
    }
    spec.update(overrides)
    path = tmp_path / "study.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def _write_obs(tmp_path: Path, rows: list[tuple]) -> Path:
    lines = ["study_id,part_id,operator_id,repeat_id,value,unit"]
    for r in rows:
        lines.append(",".join(str(x) for x in r))
    path = tmp_path / "observations.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _good_rows(study_id: str = "T") -> list[tuple]:
    rows = []
    for p, base in (("P1", 10.0), ("P2", 10.1), ("P3", 9.9)):
        for op, off in (("OP-A", 0.0), ("OP-B", 0.03)):
            for rep in (1, 2):
                rows.append((study_id, p, op, rep, f"{base + off + 0.01 * rep:.2f}", "mm"))
    return rows


# --------------------------------------------------------------------------- #
# 单位
# --------------------------------------------------------------------------- #
def test_unit_aliases():
    assert normalize_unit("mm") == UNIT_MM
    assert normalize_unit(" MM ") == UNIT_MM
    assert normalize_unit("um") == UNIT_UM
    assert normalize_unit("μm") == UNIT_UM  # 希腊 mu
    assert normalize_unit("µm") == UNIT_UM  # 微符号
    assert normalize_unit("g") is None
    assert normalize_unit("") is None


def test_convert_length():
    assert convert_length(1.0, UNIT_MM, UNIT_UM) == 1000.0
    assert convert_length(1.0, UNIT_UM, UNIT_MM) == 0.001
    assert convert_length(2.5, UNIT_MM, UNIT_MM) == 2.5
    with pytest.raises(ValueError):
        convert_length(1.0, "g", UNIT_MM)


# --------------------------------------------------------------------------- #
# 合法输入
# --------------------------------------------------------------------------- #
def test_load_demo_s1_ok():
    gage = load_gage_input(DEMO / "s1_interaction_kept" / "study.json",
                           DEMO / "s1_interaction_kept" / "observations.csv")
    assert gage.study.study_id == "GRR-S1"
    assert len(gage.observations) == 12
    assert gage.study.spec_width == pytest.approx(0.8)
    # 行号从 2 开始（第 1 行是表头）
    assert [o.row for o in gage.observations] == list(range(2, 14))


def test_load_with_unit_conversion(tmp_path):
    study = _write_study(tmp_path, unit="mm")
    rows = _good_rows()
    rows[0] = ("T", "P1", "OP-A", 1, "10010", "μm")  # 10.01 mm
    obs = _write_obs(tmp_path, rows)
    gage = load_gage_input(study, obs)
    first = [o for o in gage.observations if o.row == 2][0]
    assert first.unit == UNIT_UM
    assert first.canonical_value == pytest.approx(10.01)


# --------------------------------------------------------------------------- #
# 输入错误（INVALID_INPUT）
# --------------------------------------------------------------------------- #
def test_reject_duplicate_cell_row(tmp_path):
    study = _write_study(tmp_path)
    rows = _good_rows()
    rows.append(("T", "P1", "OP-A", 1, "10.05", "mm"))  # 与第 2 行同格
    obs = _write_obs(tmp_path, rows)
    with pytest.raises(GageInputError) as exc:
        load_gage_input(study, obs)
    msg = "\n".join(exc.value.problems)
    assert "重复行" in msg and "第14行" in msg and "第 2 行" in msg


def test_reject_undeclared_part_and_operator(tmp_path):
    study = _write_study(tmp_path)
    rows = _good_rows()
    rows[0] = ("T", "P9", "OP-A", 1, "10.01", "mm")
    rows[1] = ("T", "P1", "OP-C", 2, "10.02", "mm")
    obs = _write_obs(tmp_path, rows)
    with pytest.raises(GageInputError) as exc:
        load_gage_input(study, obs)
    msg = "\n".join(exc.value.problems)
    assert "P9" in msg and "未在试验定义中声明" in msg
    assert "OP-C" in msg


@pytest.mark.parametrize("bad", ["NaN", "inf", "-inf"])
def test_reject_non_finite_reading(tmp_path, bad):
    study = _write_study(tmp_path)
    rows = _good_rows()
    rows[3] = ("T", "P1", "OP-B", 2, bad, "mm")
    obs = _write_obs(tmp_path, rows)
    with pytest.raises(GageInputError) as exc:
        load_gage_input(study, obs)
    assert "必须有限" in "\n".join(exc.value.problems)


def test_reject_incompatible_unit(tmp_path):
    study = _write_study(tmp_path)
    rows = _good_rows()
    rows[2] = ("T", "P1", "OP-B", 1, "10.03", "g")
    obs = _write_obs(tmp_path, rows)
    with pytest.raises(GageInputError) as exc:
        load_gage_input(study, obs)
    assert "单位不明" in "\n".join(exc.value.problems)


def test_reject_repeat_out_of_range_and_non_integer(tmp_path):
    study = _write_study(tmp_path)
    rows = _good_rows()
    rows[0] = ("T", "P1", "OP-A", 3, "10.01", "mm")   # 超出 repeats=2
    rows[1] = ("T", "P1", "OP-A", "x", "10.02", "mm")  # 非整数
    obs = _write_obs(tmp_path, rows)
    with pytest.raises(GageInputError) as exc:
        load_gage_input(study, obs)
    msg = "\n".join(exc.value.problems)
    assert "超出声明的重复次数" in msg and "不是整数" in msg


def test_reject_study_id_mismatch(tmp_path):
    study = _write_study(tmp_path)
    obs = _write_obs(tmp_path, _good_rows(study_id="OTHER"))
    with pytest.raises(GageInputError) as exc:
        load_gage_input(study, obs)
    assert "不一致" in "\n".join(exc.value.problems)


def test_reject_bad_spec(tmp_path):
    # 倒置
    study = _write_study(tmp_path, spec={"lower": 10.2, "upper": 9.8})
    with pytest.raises(GageInputError) as exc:
        load_gage_input(study, _write_obs(tmp_path, _good_rows()))
    assert "规格宽度必须为正" in "\n".join(exc.value.problems)
    # 只给一侧
    study2 = _write_study(tmp_path, spec={"lower": 9.8})
    with pytest.raises(GageInputError) as exc2:
        load_gage_input(study2, _write_obs(tmp_path, _good_rows()))
    assert "同时给出" in "\n".join(exc2.value.problems)


def test_reject_duplicate_declaration(tmp_path):
    study = _write_study(tmp_path, parts=["P1", "P2", "P3", "P1"])
    with pytest.raises(GageInputError) as exc:
        load_gage_input(study, _write_obs(tmp_path, _good_rows()))
    assert "重复声明" in "\n".join(exc.value.problems)


def test_reject_missing_column(tmp_path):
    study = _write_study(tmp_path)
    path = tmp_path / "observations.csv"
    path.write_text("study_id,part_id,operator_id,repeat_id,value\n"
                    "T,P1,OP-A,1,10.01\n", encoding="utf-8")
    with pytest.raises(GageInputError) as exc:
        load_gage_input(study, path)
    assert "缺少列" in "\n".join(exc.value.problems)


# --------------------------------------------------------------------------- #
# 设计不完整（INCOMPLETE_DESIGN）
# --------------------------------------------------------------------------- #
def test_reject_missing_cell(tmp_path):
    study = _write_study(tmp_path)
    rows = [r for r in _good_rows() if not (r[1] == "P3" and r[2] == "OP-B" and r[3] == 2)]
    obs = _write_obs(tmp_path, rows)
    with pytest.raises(GageDesignError) as exc:
        load_gage_input(study, obs)
    msg = "\n".join(exc.value.problems)
    assert "缺格" in msg and "P3" in msg and "OP-B" in msg


def test_reject_missing_whole_operator(tmp_path):
    """声明了 OP-B 却没有任何 OP-B 的行：缺格，不能悄悄少算一个人。"""
    study = _write_study(tmp_path)
    rows = [r for r in _good_rows() if r[2] == "OP-A"]
    obs = _write_obs(tmp_path, rows)
    with pytest.raises(GageDesignError) as exc:
        load_gage_input(study, obs)
    assert "缺格" in "\n".join(exc.value.problems)


def test_reject_too_few_parts_operators_repeats(tmp_path):
    study = _write_study(tmp_path, parts=["P1", "P2"])
    rows = [r for r in _good_rows() if r[1] != "P3"]
    with pytest.raises(GageDesignError) as exc:
        load_gage_input(study, _write_obs(tmp_path, rows))
    assert "少于 3 个" in "\n".join(exc.value.problems)

    study = _write_study(tmp_path, operators=["OP-A"])
    rows = [r for r in _good_rows() if r[2] == "OP-A"]
    with pytest.raises(GageDesignError) as exc2:
        load_gage_input(study, _write_obs(tmp_path, rows))
    assert "少于 2 个" in "\n".join(exc2.value.problems)

    study = _write_study(tmp_path, repeats=1)
    rows = [r for r in _good_rows() if r[3] == 1]
    with pytest.raises(GageDesignError) as exc3:
        load_gage_input(study, _write_obs(tmp_path, rows))
    assert "少于 2 次" in "\n".join(exc3.value.problems)


def test_repeats_greater_than_two_ok(tmp_path):
    """重复次数可以大于 2。"""
    study = _write_study(tmp_path, repeats=3)
    rows = []
    for p, base in (("P1", 10.0), ("P2", 10.1), ("P3", 9.9)):
        for op, off in (("OP-A", 0.0), ("OP-B", 0.03)):
            for rep in (1, 2, 3):
                rows.append(("T", p, op, rep, f"{base + off + 0.01 * rep:.2f}", "mm"))
    gage = load_gage_input(study, _write_obs(tmp_path, rows))
    assert len(gage.observations) == 18
    assert gage.study.repeats == 3
