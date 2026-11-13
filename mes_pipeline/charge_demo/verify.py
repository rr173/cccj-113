"""配料结果的独立复核脚本（普通 Python，不依赖求解器结论）。

子命令：
- ``verify-plan``       逐项重算某次成功方案的费用、总重、最坏成分、库存与证书；
- ``verify-exhaustive`` 对小型整桶场景穷举全部桶数组合，核对最优费用；
- ``verify-iis``        对不可行结果的冲突约束集逐条移除后重新求解，验证解释成立；
- ``verify-midpoint``   演示“区间取中点”会如何误判检出限场景（对照用，非正式口径）；
- ``verify-all``        对演示仓库依次执行全部复核。

用法（在仓库根目录）：
    .venv/bin/python -I charge_demo/verify.py verify-plan --store /tmp/charge_store --calc-id demo-s1_bulk_optimum
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import sys
from pathlib import Path

# 允许直接以脚本方式运行（项目包已 editable 安装时无需此步）
try:
    import mes_pipeline.charge  # noqa: F401
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOL = 1e-6
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# --------------------------------------------------------------------------- #
# 独立解析（不复用项目 io 模块，保证复核路径独立）
# --------------------------------------------------------------------------- #
def _to_fraction(value: float, unit: str) -> float:
    return value / 100.0 if unit == "pct" else value / 1.0e6


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _load_inputs(result: dict):
    inp = result["input"]
    inventory = {r["lot_id"]: r for r in _read_csv(Path(inp["inventory"]))}
    certs = {r["cert_id"] for r in _read_csv(Path(inp["certificates"]))}
    assays: dict[tuple[str, str], list[dict]] = {}
    for r in _read_csv(Path(inp["assays"])):
        assays.setdefault((r["lot_id"], r["element"]), []).append(r)
    heat = json.loads(Path(inp["heat"]).read_text(encoding="utf-8"))
    return inventory, certs, assays, heat


def _interval(rows: list[dict]) -> tuple[float, float, set[str]]:
    """把同批同元素的多条化验记录合并为质量分数闭区间（与正式口径一致）。"""
    lo, hi, certs = 0.0, math.inf, set()
    for r in rows:
        certs.add(r["cert_id"])
        vt = r["value_type"].strip().lower()
        unit = r["unit"].strip().lower()
        if vt == "single":
            a = b = float(r["value"])
        elif vt == "range":
            a, b = float(r["lower"]), float(r["upper"])
        elif vt == "limit":
            a, b = 0.0, float(r["upper"])
        else:  # pragma: no cover
            raise ValueError(f"未知值类型 {vt}")
        lo = max(lo, _to_fraction(a, unit))
        hi = min(hi, _to_fraction(b, unit))
    return lo, hi, certs


def _heat_limits(heat: dict) -> dict[str, dict]:
    out = {}
    for elem, spec in heat.get("elements", {}).items():
        unit = "pct" if any(k.endswith("pct") for k in spec) else "ppm"
        out[elem] = {
            "unit": unit,
            "min": (_to_fraction(spec[f"min_{unit}"], unit)
                    if f"min_{unit}" in spec else None),
            "max": (_to_fraction(spec[f"max_{unit}"], unit)
                    if f"max_{unit}" in spec else None),
        }
    return out


# --------------------------------------------------------------------------- #
# verify-plan：逐项重算成功方案
# --------------------------------------------------------------------------- #
def cmd_verify_plan(args: argparse.Namespace) -> int:
    result = json.loads((Path(args.store) / "charge_calcs" / f"{args.calc_id}.json")
                        .read_text(encoding="utf-8"))
    if result["status"] != "OPTIMAL":
        print(f"calc {args.calc_id} 状态为 {result['status']}，仅支持复核 OPTIMAL 方案")
        return 2
    inventory, certs, assays, heat = _load_inputs(result)
    limits = _heat_limits(heat)
    mass = float(heat["total_mass_kg"])
    plan = result["plan"]
    lines = {l["lot_id"]: l for l in plan["lines"]}

    print(f"== 复核方案 {args.calc_id}（炉次 {heat['heat_id']}）==")

    # 1) 逐批费用与总费用
    total_cost = 0.0
    total_kg = 0.0
    for lot_id, line in lines.items():
        inv = inventory[lot_id]
        price = float(inv["price_per_kg"])
        kg = float(line["kg"])
        total_cost += kg * price
        total_kg += kg
        check(f"{lot_id} 费用 = kg×单价", abs(line["cost"] - kg * price) < 1e-4,
              f"{line['cost']} vs {kg * price}")
        if inv["mode"] == "drum":
            net = float(inv["drum_net_kg"])
            drums = kg / net
            check(f"{lot_id} 整桶整数", abs(drums - round(drums)) < 1e-6
                  and line["drums"] == round(drums),
                  f"{kg} kg / {net} kg = {drums}")
            usable = math.floor(float(inv["available_kg"]) / net + 1e-9) * net
        else:
            usable = float(inv["available_kg"])
        check(f"{lot_id} 不超可用库存", kg <= usable + TOL, f"{kg} <= {usable}")
    check("总投料量守恒", abs(total_kg - mass) < 1e-4, f"{total_kg} vs {mass}")
    check("总费用一致", abs(total_cost - plan["total_cost"]) < 1e-4,
          f"{total_cost} vs {plan['total_cost']}")

    # 2) 互斥约束
    for a, b in heat.get("mutex", []):
        ka = lines.get(a, {}).get("kg", 0.0)
        kb = lines.get(b, {}).get("kg", 0.0)
        check(f"互斥 {a}/{b}", not (ka > TOL and kb > TOL), f"{ka} / {kb}")

    # 3) 最坏成分（保守口径：min 用下界、max 用上界）与约束余量
    reported = {e["element"]: e for e in plan["elements"]}
    for elem, lim in limits.items():
        lo_sum = hi_sum = 0.0
        for lot_id, line in lines.items():
            lo, hi, _ = _interval(assays[(lot_id, elem)])
            lo_sum += line["kg"] * lo
            hi_sum += line["kg"] * hi
        unit = lim["unit"]
        scale = 100.0 if unit == "pct" else 1.0e6
        worst_min = lo_sum / mass * scale
        worst_max = hi_sum / mass * scale
        rep = reported[elem]
        if lim["min"] is not None:
            check(f"{elem} 最坏下界满足最低要求", worst_min >= lim["min"] * scale - 1e-4,
                  f"{worst_min:.6g} >= {lim['min'] * scale:g}")
            check(f"{elem} 报告下界一致", abs(rep["worst_min"] - worst_min) < 1e-4,
                  f"{rep['worst_min']} vs {worst_min}")
        if lim["max"] is not None:
            check(f"{elem} 最坏上界满足最高要求", worst_max <= lim["max"] * scale + 1e-4,
                  f"{worst_max:.6g} <= {lim['max'] * scale:g}")
            check(f"{elem} 报告上界一致", abs(rep["worst_max"] - worst_max) < 1e-4,
                  f"{rep['worst_max']} vs {worst_max}")

    # 4) 未用库存
    for row in plan["unused_inventory"]:
        lot_id = row["lot_id"]
        used = lines.get(lot_id, {}).get("kg", 0.0)
        check(f"{lot_id} 未用库存一致",
              abs(row["used_kg"] - used) < 1e-4
              and abs(row["remaining_usable_kg"]
                      - (_usable(inventory[lot_id]) - used)) < 1e-4)

    # 5) 使用过的化验证书：登记在册且与化验表一致
    for entry in plan["certificates_used"]:
        lot_id, elem, cert = entry["lot_id"], entry["element"], entry["cert_id"]
        check(f"证书 {cert} 有效且对应 {lot_id}/{elem}",
              cert in certs and any(r["cert_id"] == cert
                                    for r in assays.get((lot_id, elem), [])))
    return 0


def _usable(inv_row: dict) -> float:
    avail = float(inv_row["available_kg"])
    if inv_row["mode"] == "drum":
        net = float(inv_row["drum_net_kg"])
        return math.floor(avail / net + 1e-9) * net
    return avail


# --------------------------------------------------------------------------- #
# verify-exhaustive：小型整桶场景穷举核对最优费用
# --------------------------------------------------------------------------- #
def cmd_verify_exhaustive(args: argparse.Namespace) -> int:
    scenario = Path(args.scenario)
    inventory = {r["lot_id"]: r for r in _read_csv(scenario / "inventory.csv")}
    heat = json.loads((scenario / "heat.json").read_text(encoding="utf-8"))
    assays: dict[tuple[str, str], list[dict]] = {}
    for r in _read_csv(scenario / "assays.csv"):
        assays.setdefault((r["lot_id"], r["element"]), []).append(r)
    limits = _heat_limits(heat)
    mass = float(heat["total_mass_kg"])

    drums = [k for k, v in inventory.items() if v["mode"] == "drum"]
    bulks = [k for k, v in inventory.items() if v["mode"] == "bulk"]
    if len(bulks) > 1:
        print("穷举脚本仅支持至多一个散装批（其余由质量守恒确定）")
        return 2
    print(f"== 穷举核对 {scenario.name}：整桶批 {drums}，散装批 {bulks} ==")

    best = None
    best_combo = None
    ranges = []
    for d in drums:
        net = float(inventory[d]["drum_net_kg"])
        max_drums = int(float(inventory[d]["available_kg"]) // net)
        ranges.append(range(max_drums + 1))
    combos = 0
    for counts in itertools.product(*ranges):
        kg = {d: n * float(inventory[d]["drum_net_kg"]) for d, n in zip(drums, counts)}
        rest = mass - sum(kg.values())
        if bulks:
            b = bulks[0]
            if rest < -TOL or rest > float(inventory[b]["available_kg"]) + TOL:
                continue
            kg[b] = rest
        elif abs(rest) > TOL:
            continue
        combos += 1
        # 保守口径逐项检查元素约束
        feasible = True
        for elem, lim in limits.items():
            lo_sum = hi_sum = 0.0
            for lot_id, x in kg.items():
                lo, hi, _ = _interval(assays[(lot_id, elem)])
                lo_sum += x * lo
                hi_sum += x * hi
            if lim["min"] is not None and lo_sum < lim["min"] * mass - 1e-9:
                feasible = False
            if lim["max"] is not None and hi_sum > lim["max"] * mass + 1e-9:
                feasible = False
        if not feasible:
            continue
        cost = sum(kg[l] * float(inventory[l]["price_per_kg"]) for l in kg)
        if best is None or cost < best - 1e-9:
            best = cost
            best_combo = dict(kg)
    print(f"  有效组合 {combos} 个，穷举最优费用 {best}")
    check("穷举得到可行解", best is not None)

    result = json.loads((Path(args.store) / "charge_calcs" / f"{args.calc_id}.json")
                        .read_text(encoding="utf-8"))
    solver_cost = result["plan"]["total_cost"]
    print(f"  求解器最优费用 {solver_cost}")
    check("求解器费用等于穷举最优", abs(solver_cost - best) < 1e-4,
          f"{solver_cost} vs {best}")
    for lot_id, line in ((l["lot_id"], l) for l in result["plan"]["lines"]):
        check(f"{lot_id} 投料量与穷举最优一致",
              abs(line["kg"] - best_combo.get(lot_id, 0.0)) < 1e-4,
              f"{line['kg']} vs {best_combo.get(lot_id, 0.0)}")
    return 0


# --------------------------------------------------------------------------- #
# verify-iis：冲突约束集逐条移除后重新求解
# --------------------------------------------------------------------------- #
def cmd_verify_iis(args: argparse.Namespace) -> int:
    from mes_pipeline.charge.io import load_charge_input
    from mes_pipeline.charge.service import build_spec
    from mes_pipeline.charge.solver import MILP_INFEASIBLE, MILP_OPTIMAL, solve_milp

    result = json.loads((Path(args.store) / "charge_calcs" / f"{args.calc_id}.json")
                        .read_text(encoding="utf-8"))
    if result["status"] != "INFEASIBLE":
        print(f"calc {args.calc_id} 状态为 {result['status']}，仅支持复核 INFEASIBLE 结果")
        return 2
    inp = result["input"]
    charge = load_charge_input(inp["inventory"], inp["assays"],
                               inp["certificates"], inp["heat"])
    spec, _ = build_spec(charge)
    iis = [c["cid"] for c in result["conflict"]["constraints"]]
    print(f"== 复核冲突约束集 {args.calc_id}（{len(iis)} 条）==")

    out = solve_milp(spec, set(iis))
    check("冲突集整体不可行", out.status == MILP_INFEASIBLE, out.status)
    for cid in iis:
        trial = set(iis) - {cid}
        out = solve_milp(spec, trial)
        check(f"移除 {cid} 后恢复可行", out.status == MILP_OPTIMAL, out.status)
    return 0


# --------------------------------------------------------------------------- #
# verify-midpoint：演示中点法误判（对照实验，非正式口径）
# --------------------------------------------------------------------------- #
def cmd_verify_midpoint(args: argparse.Namespace) -> int:
    from mes_pipeline.charge.io import load_charge_input
    from mes_pipeline.charge.models import AssayInterval, ChargeInput
    from mes_pipeline.charge.service import build_spec
    from mes_pipeline.charge.solver import MILP_OPTIMAL, solve_milp

    scenario = Path(args.scenario)
    charge = load_charge_input(scenario / "inventory.csv", scenario / "assays.csv",
                               scenario / "certificates.csv", scenario / "heat.json")
    print(f"== 中点法对照 {scenario.name} ==")

    spec, _ = build_spec(charge)
    conservative = solve_milp(spec)
    print(f"  保守口径（区间上下界）: {conservative.status}")

    collapsed = tuple(
        AssayInterval(lot_id=a.lot_id, element=a.element,
                      lo=(a.lo + a.hi) / 2.0, hi=(a.lo + a.hi) / 2.0,
                      cert_ids=a.cert_ids)
        for a in charge.assays
    )
    mid_charge = ChargeInput(lots=charge.lots, assays=collapsed, heat=charge.heat)
    mid_spec, _ = build_spec(mid_charge)
    midpoint = solve_milp(mid_spec)
    print(f"  中点口径（区间收缩为中点）: {midpoint.status}"
          + (f"，费用 {midpoint.objective}" if midpoint.status == MILP_OPTIMAL else ""))

    check("保守口径判不可行", conservative.status != MILP_OPTIMAL, conservative.status)
    check("中点口径误判为可行", midpoint.status == MILP_OPTIMAL, midpoint.status)
    if midpoint.status == MILP_OPTIMAL:
        # 把中点方案代回保守口径复核：真实成分取区间上界时必然突破炉次上限
        heat = charge.heat
        assays = charge.assay_map()
        mass = heat.total_mass_kg
        for elem in heat.elements:
            his = [assays[(lot.lot_id, elem.element)].hi for lot in charge.lots]
            worst = sum(x * h for x, h in zip(midpoint.x, his)) / mass
            if elem.req_max is not None:
                from mes_pipeline.charge.models import from_fraction
                w = from_fraction(worst, elem.unit)
                r = from_fraction(elem.req_max, elem.unit)
                check(f"中点方案在最坏上界下突破 {elem.element} 上限",
                      worst > elem.req_max + 1e-9,
                      f"最坏 {w:.6g} {elem.unit} > 上限 {r:g} {elem.unit}")
    return 0


# --------------------------------------------------------------------------- #
# verify-all：演示仓库全量复核
# --------------------------------------------------------------------------- #
def cmd_verify_all(args: argparse.Namespace) -> int:
    store = Path(args.store)
    demo = Path(__file__).resolve().parent
    steps = [
        ("verify-plan", ["--store", str(store), "--calc-id", c])
        for c in ("demo-s1_bulk_optimum", "demo-s2_drum_relaxation", "demo-s3_fixed",
                  "demo-s4_mutex", "demo-s6_mixed_units")
    ]
    steps += [
        ("verify-exhaustive", ["--scenario", str(demo / "s2_drum_relaxation"),
                               "--store", str(store), "--calc-id", "demo-s2_drum_relaxation"]),
        ("verify-iis", ["--store", str(store), "--calc-id", "demo-s3_detection_limit"]),
        ("verify-iis", ["--store", str(store), "--calc-id", "demo-s5_shortage"]),
        ("verify-midpoint", ["--scenario", str(demo / "s3_detection_limit")]),
    ]
    rc = 0
    for cmd, extra in steps:
        print()
        ns = build_parser().parse_args([cmd] + extra)
        rc |= ns.func(ns)
    print()
    if FAILURES:
        print(f"复核失败 {len(FAILURES)} 项:")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("全部复核通过")
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("verify-plan", help="逐项重算成功方案")
    p.add_argument("--store", required=True)
    p.add_argument("--calc-id", required=True)
    p.set_defaults(func=cmd_verify_plan)

    p = sub.add_parser("verify-exhaustive", help="穷举核对小型整桶场景")
    p.add_argument("--scenario", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--calc-id", required=True)
    p.set_defaults(func=cmd_verify_exhaustive)

    p = sub.add_parser("verify-iis", help="逐条移除冲突约束并重新求解")
    p.add_argument("--store", required=True)
    p.add_argument("--calc-id", required=True)
    p.set_defaults(func=cmd_verify_iis)

    p = sub.add_parser("verify-midpoint", help="中点法误判对照")
    p.add_argument("--scenario", required=True)
    p.set_defaults(func=cmd_verify_midpoint)

    p = sub.add_parser("verify-all", help="全部复核")
    p.add_argument("--store", required=True)
    p.set_defaults(func=cmd_verify_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc = args.func(args)
    if FAILURES:
        print(f"\n复核失败 {len(FAILURES)} 项:", file=sys.stderr)
        for f in FAILURES:
            print("  -", f, file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
