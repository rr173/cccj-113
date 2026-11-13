"""求解器测试：两阶段单纯形、分支定界、互斥、冲突约束集。"""
from __future__ import annotations

import pytest

from mes_pipeline.charge.solver import (
    LP_INFEASIBLE,
    LP_OPTIMAL,
    MILP_INFEASIBLE,
    MILP_NODE_LIMIT,
    MILP_OPTIMAL,
    MilpSpec,
    NamedRow,
    extract_iis,
    solve_lp,
    solve_milp,
)


# --------------------------------------------------------------------------- #
# LP
# --------------------------------------------------------------------------- #
def test_lp_vertex_optimum():
    # min 2x+y, x+y=10, x>=4, x<=6 -> x=4, y=6, obj=14
    st, obj, x = solve_lp([2, 1], [([1, 1], 10, "="), ([-1, 0], -4, "<="), ([1, 0], 6, "<=")])
    assert st == LP_OPTIMAL
    assert obj == pytest.approx(14.0)
    assert x[0] == pytest.approx(4.0)
    assert x[1] == pytest.approx(6.0)


def test_lp_infeasible():
    st, _, _ = solve_lp([1], [([1], 1, "<="), ([-1], -2, "<=")])
    assert st == LP_INFEASIBLE


def test_lp_redundant_equality():
    # min x, x+y=10, 2x+2y=20（冗余）, x>=4 -> x=4
    st, obj, x = solve_lp([1, 0], [([1, 1], 10, "="), ([2, 2], 20, "="), ([-1, 0], -4, "<=")])
    assert st == LP_OPTIMAL
    assert obj == pytest.approx(4.0)


def test_lp_degenerate_ties_terminate():
    # Bland 规则下退化问题也必须收敛：min -x-y, x+y<=4, x<=2, y<=2 -> obj -4
    st, obj, _ = solve_lp([-1, -1], [([1, 1], 4, "<="), ([1, 0], 2, "<="), ([0, 1], 2, "<=")])
    assert st == LP_OPTIMAL
    assert obj == pytest.approx(-4.0)


# --------------------------------------------------------------------------- #
# MILP：整桶整数约束
# --------------------------------------------------------------------------- #
def test_milp_drum_integrality():
    # x=25n, x+y=60, min 2x+5y -> x=50, y=10, obj=150（松弛解 x=60 不可落地）
    spec = MilpSpec(
        objective=(2.0, 5.0),
        rows=(NamedRow("mass", (1.0, 1.0), 60.0, "="),),
        integer_steps={"int:x": (0, 25.0)},
    )
    out = solve_milp(spec)
    assert out.status == MILP_OPTIMAL
    assert out.objective == pytest.approx(150.0)
    assert out.x[0] == pytest.approx(50.0)
    assert out.x[1] == pytest.approx(10.0)


def test_milp_mutex():
    # min x+y, x+y=10, 互斥 -> 恰有一个非零
    spec = MilpSpec(
        objective=(1.0, 1.0),
        rows=(NamedRow("mass", (1.0, 1.0), 10.0, "="),),
        mutex_pairs={"mutex:x|y": (0, 1)},
    )
    out = solve_milp(spec)
    assert out.status == MILP_OPTIMAL
    assert out.objective == pytest.approx(10.0)
    assert min(out.x) == pytest.approx(0.0)


def test_milp_infeasible_status():
    spec = MilpSpec(
        objective=(1.0, 1.0),
        rows=(NamedRow("mass", (1.0, 1.0), 10.0, "="),
              NamedRow("avail:x", (1.0, 0.0), 3.0, "<="),
              NamedRow("avail:y", (0.0, 1.0), 4.0, "<=")),
    )
    assert solve_milp(spec).status == MILP_INFEASIBLE


def test_milp_node_limit_never_reported_optimal():
    spec = MilpSpec(
        objective=(2.0, 5.0),
        rows=(NamedRow("mass", (1.0, 1.0), 60.0, "="),),
        integer_steps={"int:x": (0, 25.0)},
    )
    out = solve_milp(spec, node_limit=1)
    assert out.status == MILP_NODE_LIMIT


# --------------------------------------------------------------------------- #
# 冲突约束集（IIS）
# --------------------------------------------------------------------------- #
def test_iis_minimal_and_verifiable():
    spec = MilpSpec(
        objective=(1.0, 1.0),
        rows=(NamedRow("mass", (1.0, 1.0), 10.0, "="),
              NamedRow("avail:x", (1.0, 0.0), 3.0, "<="),
              NamedRow("avail:y", (0.0, 1.0), 4.0, "<=")),
    )
    iis = extract_iis(spec)
    assert set(iis) == {"mass", "avail:x", "avail:y"}
    # 逐条移除后必须恢复可行（可重新求解验证）
    for cid in iis:
        trial = set(iis) - {cid}
        assert solve_milp(spec, trial).status == MILP_OPTIMAL


def test_iis_includes_integrality_when_binding():
    # x 必须为 25 的整数倍，x=10 -> 只有去掉整桶约束才可行
    spec = MilpSpec(
        objective=(1.0,),
        rows=(NamedRow("mass", (1.0,), 10.0, "="),),
        integer_steps={"int:x": (0, 25.0)},
    )
    iis = extract_iis(spec)
    assert set(iis) == {"mass", "int:x"}
    assert solve_milp(spec, {"mass"}).status == MILP_OPTIMAL


def test_iis_rejects_feasible_model():
    spec = MilpSpec(
        objective=(1.0,),
        rows=(NamedRow("mass", (1.0,), 10.0, "="),),
    )
    with pytest.raises(ValueError, match="并非不可行"):
        extract_iis(spec)
