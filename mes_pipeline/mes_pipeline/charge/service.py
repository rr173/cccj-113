"""配料计算编排：建模、求解、保守口径核算、结果落盘与读取。

计算口径（与需求逐条对应）：
- 成分区间不确定性按保守口径处理：最低含量约束用各原料区间**下界**核算，
  最高含量约束用**上界**核算，保证区间内任何真实成分都不会突破炉次要求；
  绝不把区间收缩成平均数/中点后再下结论；
- 整桶原料按净重整数桶投料，不足一桶的库存不参与求解；
- 互斥原料对不得同时非零投料；
- 目标函数为总投料费用；存在多个最优方案时给出各批次的等量区间说明等价性；
- 不可行时提取极小冲突约束集（IIS），可重新求解逐条验证；
- 输入不合法与约束无解是两种不同状态，分别报告。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .io import ChargeInputError, digest_files, load_charge_input
from .models import (
    MODE_DRUM,
    STATUS_INFEASIBLE,
    STATUS_INVALID,
    STATUS_NODE_LIMIT,
    STATUS_OPTIMAL,
    ChargeInput,
    from_fraction,
)
from .solver import (
    MILP_INFEASIBLE,
    MILP_NODE_LIMIT,
    MILP_OPTIMAL,
    MilpSpec,
    NamedRow,
    extract_iis,
    solve_lp,
    solve_milp,
)

CALC_DIR = "charge_calcs"
_COST_CAP_RTOL = 1e-12   # 最优费用上限的相对容差（用于等价方案区间）
_ALT_KG_TOL = 1e-4       # 判定“存在多个最优方案”的投料量区间宽度阈值
_KG_TOL = 1e-6


def _r6(value: Optional[float]) -> Optional[float]:
    """结果展示统一保留 6 位小数（None 透传）。"""
    return None if value is None else round(float(value), 6)


# --------------------------------------------------------------------------- #
# 建模
# --------------------------------------------------------------------------- #
def build_spec(charge: ChargeInput) -> Tuple[MilpSpec, Dict[str, str]]:
    """把校验过的输入转成 MILP 模型；同时返回每条约束的可读描述。"""
    lots = charge.lots
    heat = charge.heat
    n = len(lots)
    mass = heat.total_mass_kg
    assays = charge.assay_map()

    rows: List[NamedRow] = []
    desc: Dict[str, str] = {}

    rows.append(NamedRow("mass_balance", tuple([1.0] * n), mass, "="))
    desc["mass_balance"] = f"质量守恒：总投料量必须等于 {mass:g} kg"

    for elem in heat.elements:
        lo_coeffs = tuple(assays[(lot.lot_id, elem.element)].lo for lot in lots)
        hi_coeffs = tuple(assays[(lot.lot_id, elem.element)].hi for lot in lots)
        if elem.req_min is not None:
            cid = f"element_min:{elem.element}"
            rows.append(NamedRow(cid, tuple(-v for v in lo_coeffs),
                                 -elem.req_min * mass, "<="))
            desc[cid] = (
                f"元素 {elem.element} 最低含量 ≥ "
                f"{from_fraction(elem.req_min, elem.unit):g} {elem.unit}"
                f"（按各原料区间下界保守核算）"
            )
        if elem.req_max is not None:
            cid = f"element_max:{elem.element}"
            rows.append(NamedRow(cid, hi_coeffs, elem.req_max * mass, "<="))
            desc[cid] = (
                f"元素 {elem.element} 最高含量 ≤ "
                f"{from_fraction(elem.req_max, elem.unit):g} {elem.unit}"
                f"（按各原料区间上界保守核算）"
            )

    integer_steps: Dict[str, Tuple[int, float]] = {}
    for i, lot in enumerate(lots):
        cid = f"availability:{lot.lot_id}"
        rows.append(NamedRow(cid, tuple(1.0 if k == i else 0.0 for k in range(n)),
                             lot.usable_kg, "<="))
        desc[cid] = f"批次 {lot.lot_id} 可用库存上限 {lot.usable_kg:g} kg"
        if lot.mode == MODE_DRUM:
            cid_int = f"integrality:{lot.lot_id}"
            integer_steps[cid_int] = (i, lot.drum_net_kg or 0.0)
            desc[cid_int] = (f"批次 {lot.lot_id} 必须整桶投料"
                             f"（每桶净重 {lot.drum_net_kg:g} kg）")

    index = {lot.lot_id: i for i, lot in enumerate(lots)}
    mutex: Dict[str, Tuple[int, int]] = {}
    for a, b in heat.mutex_pairs:
        cid = f"mutex:{a}|{b}"
        mutex[cid] = (index[a], index[b])
        desc[cid] = f"批次 {a} 与 {b} 不得同时入炉"

    objective = tuple(lot.price_per_kg for lot in lots)
    spec = MilpSpec(objective=objective, rows=tuple(rows),
                    integer_steps=integer_steps, mutex_pairs=mutex)
    return spec, desc


# --------------------------------------------------------------------------- #
# 结果核算
# --------------------------------------------------------------------------- #
def _element_checks(charge: ChargeInput, x: Sequence[float]) -> List[dict]:
    """按保守口径核算各元素最坏上下界与约束余量（换算回炉次书写单位）。"""
    mass = charge.heat.total_mass_kg
    assays = charge.assay_map()
    checks: List[dict] = []
    for elem in charge.heat.elements:
        los = [assays[(lot.lot_id, elem.element)].lo for lot in charge.lots]
        his = [assays[(lot.lot_id, elem.element)].hi for lot in charge.lots]
        worst_min = sum(xi * lo for xi, lo in zip(x, los)) / mass
        worst_max = sum(xi * hi for xi, hi in zip(x, his)) / mass
        checks.append({
            "element": elem.element,
            "unit": elem.unit,
            "worst_min": _r6(from_fraction(worst_min, elem.unit)) if elem.req_min is not None else None,
            "worst_max": _r6(from_fraction(worst_max, elem.unit)) if elem.req_max is not None else None,
            "req_min": _r6(from_fraction(elem.req_min, elem.unit)) if elem.req_min is not None else None,
            "req_max": _r6(from_fraction(elem.req_max, elem.unit)) if elem.req_max is not None else None,
            "margin_min": (_r6(from_fraction(worst_min - elem.req_min, elem.unit))
                           if elem.req_min is not None else None),
            "margin_max": (_r6(from_fraction(elem.req_max - worst_max, elem.unit))
                           if elem.req_max is not None else None),
        })
    return checks


def _kg_ranges_under_optimal(spec: MilpSpec, cost_star: float) -> List[dict]:
    """各批次在最优费用下的投料区间（用于说明多个最优方案的等价性）。"""
    n = spec.n_vars
    cap = cost_star + max(1e-9, abs(cost_star) * _COST_CAP_RTOL)
    capped = MilpSpec(
        objective=spec.objective,
        rows=spec.rows + (NamedRow("_cost_cap", spec.objective, cap, "<="),),
        integer_steps=spec.integer_steps,
        mutex_pairs=spec.mutex_pairs,
    )
    ranges = []
    for i in range(n):
        min_obj = tuple(1.0 if k == i else 0.0 for k in range(n))
        max_obj = tuple(-1.0 if k == i else 0.0 for k in range(n))
        lo_out = solve_milp(MilpSpec(min_obj, capped.rows, capped.integer_steps,
                                     capped.mutex_pairs))
        hi_out = solve_milp(MilpSpec(max_obj, capped.rows, capped.integer_steps,
                                     capped.mutex_pairs))
        lo = lo_out.objective if lo_out.status == MILP_OPTIMAL else 0.0
        hi = -hi_out.objective if hi_out.status == MILP_OPTIMAL else 0.0
        ranges.append({"kg_min": max(0.0, lo), "kg_max": max(0.0, hi)})
    return ranges


def _build_plan(charge: ChargeInput, spec: MilpSpec, x: Sequence[float],
                total_cost: float) -> dict:
    lots = charge.lots
    assays = charge.assay_map()
    lines = []
    for lot, xi in zip(lots, x):
        if xi <= _KG_TOL:
            continue
        drums = None
        if lot.mode == MODE_DRUM:
            drums = int(round(xi / (lot.drum_net_kg or 1.0)))
        lines.append({
            "lot_id": lot.lot_id,
            "material": lot.material,
            "mode": lot.mode,
            "kg": _r6(xi),
            "drums": drums,
            "price_per_kg": lot.price_per_kg,
            "cost": _r6(xi * lot.price_per_kg),
        })

    unused = [{
        "lot_id": lot.lot_id,
        "available_kg": _r6(lot.available_kg),
        "used_kg": _r6(xi),
        "remaining_usable_kg": _r6(lot.usable_kg - xi),
        "unusable_remainder_kg": _r6(lot.unusable_remainder_kg),
    } for lot, xi in zip(lots, x)]

    certs_used = []
    for lot, xi in zip(lots, x):
        if xi <= _KG_TOL:
            continue
        for elem in charge.heat.elements:
            interval = assays[(lot.lot_id, elem.element)]
            for cert_id in interval.cert_ids:
                certs_used.append({"lot_id": lot.lot_id, "element": elem.element,
                                   "cert_id": cert_id})

    ranges = _kg_ranges_under_optimal(spec, total_cost)
    alternate = any(r["kg_max"] - r["kg_min"] > _ALT_KG_TOL for r in ranges)
    range_rows = [
        {"lot_id": lot.lot_id,
         "kg_min": _r6(ranges[i]["kg_min"]), "kg_max": _r6(ranges[i]["kg_max"])}
        for i, lot in enumerate(lots)
        if ranges[i]["kg_max"] > _ALT_KG_TOL
    ]

    return {
        "total_cost": _r6(total_cost),
        "total_mass_kg": charge.heat.total_mass_kg,
        "lines": lines,
        "elements": _element_checks(charge, x),
        "unused_inventory": unused,
        "certificates_used": certs_used,
        "alternate_optima": {
            "exists": alternate,
            "note": ("存在多个最低成本方案，下列各批次投料量在给定区间内取值时"
                     "总费用相同、约束均满足" if alternate else
                     "最低成本方案唯一（各批次投料量在最优费用下无活动区间）"),
            "kg_ranges": range_rows,
        },
    }


# --------------------------------------------------------------------------- #
# 入口：求解并落盘
# --------------------------------------------------------------------------- #
def _sanitize_calc_id(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text)


def _result_path(store_dir: str | Path, calc_id: str) -> Path:
    return Path(store_dir) / CALC_DIR / f"{calc_id}.json"


def _heat_dict(charge: ChargeInput) -> dict:
    return {
        "heat_id": charge.heat.heat_id,
        "total_mass_kg": charge.heat.total_mass_kg,
        "elements": [
            {
                "element": e.element,
                "unit": e.unit,
                "req_min": _r6(from_fraction(e.req_min, e.unit)) if e.req_min is not None else None,
                "req_max": _r6(from_fraction(e.req_max, e.unit)) if e.req_max is not None else None,
            }
            for e in charge.heat.elements
        ],
        "mutex_pairs": [list(p) for p in charge.heat.mutex_pairs],
    }


def solve_charge(inventory: str | Path, assays: str | Path, certificates: str | Path,
                 heat: str | Path, store_dir: str | Path,
                 calc_id: Optional[str] = None) -> dict:
    """读取输入、求解并保存结果；任何状态都产出可读的结果 JSON。"""
    files = [Path(inventory), Path(assays), Path(certificates), Path(heat)]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    try:
        digest = digest_files(files)
    except OSError as exc:
        digest = None
        calc_id = _sanitize_calc_id(calc_id or f"invalid-{stamp}")
        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "calc_id": calc_id,
            "status": STATUS_INVALID,
            "input": {"inventory": str(inventory), "assays": str(assays),
                      "certificates": str(certificates), "heat": str(heat),
                      "sha256": None},
            "errors": [f"输入文件不可读: {exc}"],
            "note": "输入数据不合法，本次计算被拒绝；缺值未按零填充。",
        }
        _save(store_dir, calc_id, result)
        return result
    base = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input": {
            "inventory": str(inventory),
            "assays": str(assays),
            "certificates": str(certificates),
            "heat": str(heat),
            "sha256": digest,
        },
    }

    try:
        charge = load_charge_input(inventory, assays, certificates, heat)
    except ChargeInputError as exc:
        calc_id = _sanitize_calc_id(calc_id or f"invalid-{stamp}")
        result = {
            **base,
            "calc_id": calc_id,
            "status": STATUS_INVALID,
            "errors": exc.problems,
            "note": "输入数据不合法，本次计算被拒绝；缺值未按零填充。",
        }
        _save(store_dir, calc_id, result)
        return result

    calc_id = _sanitize_calc_id(calc_id or f"{charge.heat.heat_id}-{stamp}")
    spec, desc = build_spec(charge)
    outcome = solve_milp(spec)

    # 连续松弛下界（去掉整桶/互斥），用于对照展示
    lp_rows = [(list(r.coeffs), r.rhs, r.sense) for r in spec.rows]
    lp_status, lp_obj, _ = solve_lp(list(spec.objective), lp_rows)
    relaxation = _r6(lp_obj) if lp_status == "optimal" else None

    solver_info = {"nodes": outcome.nodes, "lp_solves": outcome.lp_solves,
                   "continuous_relaxation_cost": relaxation}

    if outcome.status == MILP_OPTIMAL:
        result = {
            **base,
            "calc_id": calc_id,
            "status": STATUS_OPTIMAL,
            "heat": _heat_dict(charge),
            "plan": _build_plan(charge, spec, outcome.x, outcome.objective),
            "solver": solver_info,
        }
    elif outcome.status == MILP_INFEASIBLE:
        iis = extract_iis(spec)
        result = {
            **base,
            "calc_id": calc_id,
            "status": STATUS_INFEASIBLE,
            "heat": _heat_dict(charge),
            "conflict": {
                "constraints": [{"cid": cid, "description": desc[cid]} for cid in iis],
                "note": ("上述约束集合整体不可行；移除其中任意一条即可恢复可行，"
                         "可通过重新求解逐条验证。未自动放宽任何限制。"),
            },
            "solver": solver_info,
            "note": "约束无解（与输入不合法不同）：未给出投料方案，未放宽任何约束。",
        }
    else:  # NODE_LIMIT
        result = {
            **base,
            "calc_id": calc_id,
            "status": STATUS_NODE_LIMIT,
            "heat": _heat_dict(charge),
            "solver": solver_info,
            "note": "分支定界超过节点上限，未收敛；不把当前结果当作最优解。",
        }
    _save(store_dir, calc_id, result)
    return result


def _save(store_dir: str | Path, calc_id: str, result: dict) -> None:
    path = _result_path(store_dir, calc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def load_result(store_dir: str | Path, calc_id: str) -> dict:
    """按 calc_id 读取某次计算结果（成功或失败均可读）。"""
    path = _result_path(store_dir, calc_id)
    if not path.exists():
        known = sorted(p.stem for p in path.parent.glob("*.json")) if path.parent.exists() else []
        raise KeyError(f"未找到计算结果 {calc_id!r}（已有: {known}）")
    return json.loads(path.read_text(encoding="utf-8"))
