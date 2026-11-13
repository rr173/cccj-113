"""量具 R&R ANOVA 数值核对：手工核算的平方和/自由度/方差分量/指标。

所有期望值均由手工逐项核算（见 grr_demo/README.md 的演算），与实现无关。
"""
from __future__ import annotations

import math

import pytest

from mes_pipeline.gage.anova import (
    BRANCH_POOLED,
    BRANCH_WITH_INTERACTION,
    DECISION_KEEP,
    DECISION_POOL,
    VarianceComponents,
    build_metrics,
    collect_undefined,
    run_anova,
    ss_decomposition_ok,
)

PARTS = ["P1", "P2", "P3"]
OPS = ["OP-A", "OP-B"]

# grr_demo/s1_interaction_kept：交互显著，保留交互
S1_CELLS = {
    ("P1", "OP-A"): (10.00, 10.02), ("P1", "OP-B"): (10.04, 10.06),
    ("P2", "OP-A"): (10.09, 10.11), ("P2", "OP-B"): (10.23, 10.25),
    ("P3", "OP-A"): (9.80, 9.82),   ("P3", "OP-B"): (9.84, 9.86),
}
# grr_demo/s2_interaction_pooled：交互为零，合并入误差
S2_CELLS = {
    ("P1", "OP-A"): (10.00, 10.03), ("P1", "OP-B"): (10.05, 10.04),
    ("P2", "OP-A"): (10.10, 10.13), ("P2", "OP-B"): (10.15, 10.14),
    ("P3", "OP-A"): (9.90, 9.93),   ("P3", "OP-B"): (9.95, 9.94),
}
# grr_demo/s3_negative_component：操作员分量原估计为负
S3_CELLS = {
    ("P1", "OP-A"): (10.00, 10.02), ("P1", "OP-B"): (9.99, 10.01),
    ("P2", "OP-A"): (10.10, 10.12), ("P2", "OP-B"): (10.19, 10.21),
    ("P3", "OP-A"): (9.90, 9.92),   ("P3", "OP-B"): (9.89, 9.91),
}
# grr_demo/s4_all_equal：全部读数相同，0/0 不可判定
S4_CELLS = {(p, o): (10.00, 10.00) for p in PARTS for o in OPS}
# 每格重复完全相同但格间有交互：误差均方为 0、交互均方为 0.0 以上 → F 发散
DIVERGENT_CELLS = {
    ("P1", "OP-A"): (10.00, 10.00), ("P1", "OP-B"): (10.02, 10.02),
    ("P2", "OP-A"): (10.10, 10.10), ("P2", "OP-B"): (10.14, 10.14),
    ("P3", "OP-A"): (9.90, 9.90),   ("P3", "OP-B"): (9.92, 9.92),
}


# --------------------------------------------------------------------------- #
# s1：保留交互分支，逐项手算核对
# --------------------------------------------------------------------------- #
def test_s1_anova_table():
    r = run_anova(S1_CELLS, PARTS, OPS, repeats=2, alpha=0.25)
    assert r.part.ss == pytest.approx(0.2336)
    assert r.operator.ss == pytest.approx(0.01613333, abs=1e-8)
    assert r.interaction.ss == pytest.approx(0.00666667, abs=1e-8)
    assert r.error.ss == pytest.approx(0.0012)
    assert r.total.ss == pytest.approx(0.2576)
    assert (r.part.df, r.operator.df, r.interaction.df, r.error.df,
            r.total.df) == (2, 1, 2, 6, 11)
    assert r.part.ms == pytest.approx(0.1168)
    assert r.operator.ms == pytest.approx(0.01613333, abs=1e-8)
    assert r.interaction.ms == pytest.approx(0.00333333, abs=1e-8)
    assert r.error.ms == pytest.approx(0.0002)
    assert ss_decomposition_ok(r)


def test_s1_interaction_test_and_components():
    r = run_anova(S1_CELLS, PARTS, OPS, repeats=2, alpha=0.25)
    t = r.test
    assert t.decision == DECISION_KEEP
    assert t.f_statistic == pytest.approx(16.666667, rel=1e-6)
    assert (t.df_num, t.df_den) == (2, 6)
    assert t.p_value == pytest.approx(0.0035495, rel=1e-4)
    assert t.p_value < t.alpha
    assert r.pooled_error is None

    c = r.components
    assert c.branch == BRANCH_WITH_INTERACTION
    assert c.raw["repeatability"] == pytest.approx(0.0002)
    assert c.raw["interaction"] == pytest.approx(0.00156667, abs=1e-8)
    assert c.raw["operator"] == pytest.approx(0.00213333, abs=1e-8)
    assert c.raw["part"] == pytest.approx(0.02836667, abs=1e-8)
    assert c.truncated == ()
    assert c.used == c.raw


def test_s1_metrics():
    r = run_anova(S1_CELLS, PARTS, OPS, repeats=2, alpha=0.25)
    m = build_metrics(r.components, 9.6, 10.4)
    assert m["variance"]["grr"] == pytest.approx(0.0039)
    assert m["variance"]["total"] == pytest.approx(0.03226667, abs=1e-8)
    # 方差贡献率与 6σ 标准差比例是两个口径，数值必须不同
    assert m["contribution_pct"]["grr"] == pytest.approx(12.0868, rel=1e-4)
    assert m["pct_study_var"]["grr"] == pytest.approx(34.7660, rel=1e-4)
    assert m["contribution_pct"]["part"] == pytest.approx(87.9132, rel=1e-4)
    assert m["pct_study_var"]["part"] == pytest.approx(93.7621, rel=1e-4)
    assert m["pct_tolerance_grr"] == pytest.approx(46.8375, rel=1e-4)
    # ndc = floor(1.41 * 0.168424 / 0.062450) = floor(3.80…) = 3
    assert m["ndc"] == 3
    assert collect_undefined(m) == {}


# --------------------------------------------------------------------------- #
# s2：交互合并分支
# --------------------------------------------------------------------------- #
def test_s2_pooled_branch():
    r = run_anova(S2_CELLS, PARTS, OPS, repeats=2, alpha=0.25)
    assert r.interaction.ss == pytest.approx(0.0, abs=1e-12)
    assert r.test.decision == DECISION_POOL
    assert r.test.f_statistic == pytest.approx(0.0)
    assert r.test.p_value == pytest.approx(1.0)

    pooled = r.pooled_error
    assert pooled is not None
    assert pooled.ss == pytest.approx(0.0015)
    assert pooled.df == 8
    assert pooled.ms == pytest.approx(0.0001875)

    c = r.components
    assert c.branch == BRANCH_POOLED
    assert c.raw["interaction"] is None  # 交互已合并，不再单独存在
    assert c.used["interaction"] == 0.0
    assert c.raw["repeatability"] == pytest.approx(0.0001875)
    assert c.raw["operator"] == pytest.approx(0.00041875)
    assert c.raw["part"] == pytest.approx(0.009953125)

    m = build_metrics(c, 9.8, 10.3)
    assert m["variance"]["grr"] == pytest.approx(0.00060625)
    assert m["variance"]["total"] == pytest.approx(0.010559375)
    assert m["ndc"] == 5
    assert m["pct_tolerance_grr"] == pytest.approx(29.5466, rel=1e-4)


def test_s2_ss_decomposition_includes_pooled_origin():
    r = run_anova(S2_CELLS, PARTS, OPS, repeats=2, alpha=0.25)
    # 合并误差的平方和 = 交互 + 误差，自由度同理
    assert r.pooled_error.ss == pytest.approx(r.interaction.ss + r.error.ss)
    assert r.pooled_error.df == r.interaction.df + r.error.df
    assert ss_decomposition_ok(r)


# --------------------------------------------------------------------------- #
# s3：负方差分量
# --------------------------------------------------------------------------- #
def test_s3_negative_component_kept_raw_truncated_used():
    r = run_anova(S3_CELLS, PARTS, OPS, repeats=2, alpha=0.25)
    assert r.test.decision == DECISION_KEEP  # 交互显著，保留
    c = r.components
    # 原估计为负，保留在 raw 供解释
    assert c.raw["operator"] == pytest.approx(-0.00028333, abs=1e-8)
    # 进入指标前各自独立截到零
    assert c.used["operator"] == 0.0
    assert c.truncated == ("operator",)
    # 其余分量不受影响、不按比例分摊
    assert c.used["interaction"] == pytest.approx(0.00156667, abs=1e-8)
    assert c.used["part"] == pytest.approx(0.015)

    m = build_metrics(c, None, None)
    assert m["variance"]["grr"] == pytest.approx(0.00176667, abs=1e-8)
    assert m["variance"]["total"] == pytest.approx(0.01676667, abs=1e-8)
    # 无规格：不算规格占比
    assert m["pct_tolerance_grr"] is None
    assert m["ndc"] == 4


# --------------------------------------------------------------------------- #
# 零分母规则
# --------------------------------------------------------------------------- #
def test_s4_all_equal_zero_denominator_undecidable():
    r = run_anova(S4_CELLS, PARTS, OPS, repeats=2, alpha=0.25)
    t = r.test
    assert t.f_statistic is None and t.p_value is None
    assert t.decision == DECISION_KEEP  # 0/0 不可判定，保守保留交互
    assert "不可判定" in t.reason
    c = r.components
    assert all(v == 0.0 for v in c.used.values())

    m = build_metrics(c, 9.9, 10.1)
    # 总方差为零：贡献率与标准差比例不可定义，不写成 0/无穷
    for section in ("contribution_pct", "pct_study_var"):
        for name in ("repeatability", "reproducibility", "grr", "part"):
            assert "undefined" in m[section][name]
    assert "undefined" in m["ndc"]
    # 规格占比的分母是规格宽度（正），分子为 0 → 0.0 可定义
    assert m["pct_tolerance_grr"] == 0.0
    undefined = collect_undefined(m)
    assert len(undefined) == 9  # 4 贡献率 + 4 标准差比例 + ndc


def test_divergent_f_when_error_ms_zero():
    r = run_anova(DIVERGENT_CELLS, PARTS, OPS, repeats=2, alpha=0.25)
    t = r.test
    assert r.error.ms == 0.0 and r.interaction.ms > 0.0
    assert t.f_statistic is None  # 不产出无穷大
    assert t.p_value == 0.0       # 按显著交互处理
    assert t.decision == DECISION_KEEP
    assert "发散" in t.reason
    # 不产出 NaN 后继续比较：分量估计正常完成
    assert r.components.raw["repeatability"] == 0.0
    assert r.components.raw["interaction"] > 0.0


def test_alpha_must_be_in_open_interval():
    with pytest.raises(ValueError):
        run_anova(S1_CELLS, PARTS, OPS, repeats=2, alpha=0.0)
    with pytest.raises(ValueError):
        run_anova(S1_CELLS, PARTS, OPS, repeats=2, alpha=1.0)


def test_alpha_controls_branch():
    # 同一数据：alpha=0.001 时 p≈0.0035 >= alpha → 合并；alpha=0.25 → 保留
    r_pool = run_anova(S1_CELLS, PARTS, OPS, repeats=2, alpha=0.001)
    assert r_pool.test.decision == DECISION_POOL
    r_keep = run_anova(S1_CELLS, PARTS, OPS, repeats=2, alpha=0.25)
    assert r_keep.test.decision == DECISION_KEEP


# --------------------------------------------------------------------------- #
# 指标口径
# --------------------------------------------------------------------------- #
def test_ndc_floor_uses_1_41_factor():
    # σ_P/σ_GRR = 1/0.7 → 1.41/0.7 = 2.014… → floor = 2（√2 口径同样为 2，
    # 这里用 1.41 是需求指定系数）；σ_GRR = 0 时不可定义
    comp = VarianceComponents(
        branch=BRANCH_WITH_INTERACTION,
        raw={}, used={"repeatability": 0.4, "operator": 0.09,
                      "interaction": 0.0, "part": 1.0},
        truncated=(),
    )
    m = build_metrics(comp, None, None)
    # grr = 0.49 → σ_GRR = 0.7；part = 1.0 → σ_P = 1.0
    assert m["ndc"] == math.floor(1.41 / 0.7) == 2


def test_contribution_and_study_var_are_distinct_scales():
    comp = VarianceComponents(
        branch=BRANCH_WITH_INTERACTION,
        raw={}, used={"repeatability": 1.0, "operator": 0.0,
                      "interaction": 0.0, "part": 3.0},
        truncated=(),
    )
    m = build_metrics(comp, None, None)
    # 方差占比 25% ↔ 标准差占比 50%，两者不混用
    assert m["contribution_pct"]["grr"] == pytest.approx(25.0)
    assert m["pct_study_var"]["grr"] == pytest.approx(50.0)


def test_bad_spec_width_rejected_in_metrics():
    comp = VarianceComponents(
        branch=BRANCH_WITH_INTERACTION,
        raw={}, used={"repeatability": 0.1, "operator": 0.0,
                      "interaction": 0.0, "part": 1.0},
        truncated=(),
    )
    with pytest.raises(ValueError):
        build_metrics(comp, 10.2, 9.8)
