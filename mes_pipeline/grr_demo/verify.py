"""量具 R&R 结果的独立复核脚本（不调用生产分析函数）。

复核路径与生产代码完全独立：自行解析 study.json / observations.csv，
用普通 Python（仅 F 分布生存函数用 SciPy）重算平方和、自由度、均方、
F 检验、方差分量与全部 GRR 指标，再与生产系统保存的结果 JSON 逐项比对。

子命令：
- ``verify-compute``    独立重算某次分析并与保存结果逐项核对；
- ``verify-invariance`` 对 s1 数据做交换命名/打乱行序/整体平移/mm↔μm 换算
                        核对（此核对是对生产入口的性质检验，会调用生产 CLI
                        对变换后的等价输入重算并比对统计结果）；
- ``verify-all``        对演示仓库依次执行全部复核。

用法（在仓库根目录）：
    .venv/bin/python -I grr_demo/verify.py verify-compute --store /tmp/grr_store --calc-id demo-s1
    .venv/bin/python -I grr_demo/verify.py verify-all --store /tmp/grr_store
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from scipy.stats import f as f_dist

ROOT = Path(__file__).resolve().parent.parent
DEMO = Path(__file__).resolve().parent

REL = 1e-9       # 数值比对相对容差
ABS = 1e-12      # 数值比对绝对容差
NDC_FACTOR = 1.41
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# --------------------------------------------------------------------------- #
# 独立解析（不复用项目 io 模块）
# --------------------------------------------------------------------------- #
def _to_mm(value: float, unit: str) -> float:
    u = unit.strip().lower()
    if u == "mm":
        return value
    if u in ("um", "μm", "µm"):
        return value / 1000.0
    raise ValueError(f"不兼容单位 {unit!r}")


def _to_canonical(value: float, unit: str, canonical: str) -> float:
    mm = _to_mm(value, unit)
    return mm * 1000.0 if canonical == "μm" else mm


def _load(study_path: Path, obs_path: Path):
    study = json.loads(study_path.read_text(encoding="utf-8"))
    canonical = study["unit"].strip().lower()
    if canonical in ("um", "μm", "µm"):
        canonical = "μm"
    parts = list(study["parts"])
    operators = list(study["operators"])
    repeats = int(study["repeats"])
    spec = study.get("spec")

    cells: dict[tuple[str, str], list[float]] = {}
    rows: list[dict] = []
    with open(obs_path, newline="", encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            value = float(row["value"])
            canon = _to_canonical(value, row["unit"], canonical)
            key = (row["part_id"].strip(), row["operator_id"].strip())
            cells.setdefault(key, []).append(canon)
            rows.append({
                "row": i,
                "part_id": key[0],
                "operator_id": key[1],
                "repeat_id": int(row["repeat_id"]),
                "canonical_value": canon,
            })
    return study, parts, operators, repeats, canonical, spec, cells, rows


# --------------------------------------------------------------------------- #
# 独立 ANOVA 参考计算（与生产实现互不调用）
# --------------------------------------------------------------------------- #
def _ref_anova(parts, operators, repeats, cells, alpha):
    a, b, n = len(parts), len(operators), repeats
    cell_mean = {k: math.fsum(v) / len(v) for k, v in cells.items()}
    part_mean = {p: math.fsum(cell_mean[(p, o)] for o in operators) / b
                 for p in parts}
    op_mean = {o: math.fsum(cell_mean[(p, o)] for p in parts) / a
               for o in operators}
    grand = math.fsum(cell_mean.values()) / (a * b)

    ss_p = b * n * math.fsum((part_mean[p] - grand) ** 2 for p in parts)
    ss_o = a * n * math.fsum((op_mean[o] - grand) ** 2 for o in operators)
    ss_po = n * math.fsum(
        (cell_mean[(p, o)] - part_mean[p] - op_mean[o] + grand) ** 2
        for p in parts for o in operators)
    ss_e = math.fsum((v - cell_mean[k]) ** 2 for k, vals in cells.items()
                     for v in vals)
    ss_t = math.fsum((v - grand) ** 2 for vals in cells.values() for v in vals)

    df_p, df_o, df_po, df_e = a - 1, b - 1, (a - 1) * (b - 1), a * b * (n - 1)
    ms_p, ms_o = ss_p / df_p, ss_o / df_o
    ms_po, ms_e = ss_po / df_po, ss_e / df_e

    # 交互检验（含零分母规则）
    if ms_e == 0.0:
        if ms_po == 0.0:
            f_stat, p_value, decision = None, None, "keep_interaction"
        else:
            f_stat, p_value, decision = None, 0.0, "keep_interaction"
    else:
        f_stat = ms_po / ms_e
        p_value = float(f_dist.sf(f_stat, df_po, df_e))
        decision = "keep_interaction" if p_value < alpha else "pool_into_error"

    pooled = None
    if decision == "pool_into_error":
        p_ss, p_df = ss_po + ss_e, df_po + df_e
        p_ms = p_ss / p_df
        pooled = {"ss": p_ss, "df": p_df, "ms": p_ms}
        raw = {"repeatability": p_ms, "operator": (ms_o - p_ms) / (a * n),
               "interaction": None, "part": (ms_p - p_ms) / (b * n)}
        branch = "pooled_error"
    else:
        raw = {"repeatability": ms_e, "operator": (ms_o - ms_po) / (a * n),
               "interaction": (ms_po - ms_e) / n,
               "part": (ms_p - ms_po) / (b * n)}
        branch = "with_interaction"

    used, truncated = {}, []
    for k, v in raw.items():
        if v is None:
            continue
        if v < 0.0:
            truncated.append(k)
            used[k] = 0.0
        else:
            used[k] = v
    if branch == "pooled_error":
        used["interaction"] = 0.0

    return {
        "terms": {
            "part": {"ss": ss_p, "df": df_p, "ms": ms_p},
            "operator": {"ss": ss_o, "df": df_o, "ms": ms_o},
            "interaction": {"ss": ss_po, "df": df_po, "ms": ms_po},
            "error": {"ss": ss_e, "df": df_e, "ms": ms_e},
            "total": {"ss": ss_t, "df": a * b * n - 1},
        },
        "ss_sum_of_terms": ss_p + ss_o + ss_po + ss_e,
        "test": {"f_statistic": f_stat, "p_value": p_value,
                 "decision": decision, "df_num": df_po, "df_den": df_e},
        "pooled_error": pooled,
        "branch": branch,
        "raw": raw, "used": used, "truncated": sorted(truncated),
        "means": {"grand": grand, "by_part": part_mean,
                  "by_operator": op_mean, "cells": cell_mean},
    }


def _ref_metrics(used, spec):
    ev = used["repeatability"]
    av = used["operator"] + used["interaction"]
    grr, pv = ev + av, used["part"]
    tv = grr + pv
    sd = {k: math.sqrt(v) for k, v in
          (("repeatability", ev), ("reproducibility", av), ("grr", grr),
           ("part", pv), ("total", tv))}

    def ratio(num, den):
        return None if den == 0.0 else 100.0 * num / den

    m = {
        "variance": {"repeatability": ev, "reproducibility": av, "grr": grr,
                     "part": pv, "total": tv},
        "contribution_pct": {k: ratio(v, tv) for k, v in
                             (("repeatability", ev), ("reproducibility", av),
                              ("grr", grr), ("part", pv))},
        "pct_study_var": {k: ratio(sd[k], sd["total"]) for k in
                          ("repeatability", "reproducibility", "grr", "part")},
        "pct_tolerance_grr": (None if not spec else
                              100.0 * 6.0 * sd["grr"]
                              / (spec["upper"] - spec["lower"])),
        "ndc": (None if sd["grr"] == 0.0
                else math.floor(NDC_FACTOR * sd["part"] / sd["grr"])),
    }
    return m


# --------------------------------------------------------------------------- #
# 比对
# --------------------------------------------------------------------------- #
def _close(a, b) -> bool:
    return isinstance(b, (int, float)) and not isinstance(b, bool) \
        and math.isclose(a, b, rel_tol=REL, abs_tol=ABS)


def _compare_scalar(name, expected, actual) -> bool:
    """比对单个叶子；actual 为 {"undefined": ...} 时 expected 应为 None。"""
    if isinstance(actual, dict) and set(actual) == {"undefined"}:
        ok = expected is None
        check(name, ok, "参考计算为不可定义" if ok
              else f"参考值 {expected!r}，结果标为不可定义: {actual['undefined']}")
        return ok
    if expected is None or actual is None:
        ok = expected is None and actual is None
        check(name, ok, f"参考 {expected!r} vs 结果 {actual!r}")
        return ok
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        ok = _close(float(expected), float(actual))
        check(name, ok, f"参考 {expected!r} vs 结果 {actual!r}" if not ok else "")
        return ok
    ok = expected == actual
    check(name, ok, f"参考 {expected!r} vs 结果 {actual!r}")
    return ok


def _compare_result(result: dict, ref: dict, ref_metrics: dict,
                    rows: list[dict]) -> None:
    anova = result["anova"]
    for term, expect in ref["terms"].items():
        got = anova["terms"][term]
        _compare_scalar(f"anova.{term}.ss", expect["ss"], got["ss"])
        _compare_scalar(f"anova.{term}.df", expect["df"], got["df"])
        if term != "total":
            _compare_scalar(f"anova.{term}.ms", expect["ms"], got["ms"])
    _compare_scalar("ss 分解和", ref["ss_sum_of_terms"],
                    anova["ss_decomposition_check"]["ss_sum_of_terms"])
    check("ss 分解核对 ok", anova["ss_decomposition_check"]["ok"] is True)

    t = anova["interaction_test"]
    _compare_scalar("检验 F", ref["test"]["f_statistic"], t["f_statistic"])
    _compare_scalar("检验 p", ref["test"]["p_value"], t["p_value"])
    _compare_scalar("检验自由度(分子)", ref["test"]["df_num"], t["df_num"])
    _compare_scalar("检验自由度(分母)", ref["test"]["df_den"], t["df_den"])
    _compare_scalar("分支决策", ref["test"]["decision"], t["decision"])
    if ref["pooled_error"] is None:
        check("无合并误差段", "pooled_error" not in anova)
    else:
        for k in ("ss", "df", "ms"):
            _compare_scalar(f"合并误差.{k}", ref["pooled_error"][k],
                            anova["pooled_error"][k])

    comp = result["variance_components"]
    _compare_scalar("分量分支", ref["branch"], comp["branch"])
    for k, v in ref["raw"].items():
        _compare_scalar(f"raw.{k}", v, comp["raw"][k])
    for k, v in ref["used"].items():
        _compare_scalar(f"used.{k}", v, comp["used"][k])
    _compare_scalar("截断清单", ref["truncated"],
                    sorted(comp["truncated_to_zero"]))

    m = result["metrics"]
    for k, v in ref_metrics["variance"].items():
        _compare_scalar(f"方差.{k}", v, m["variance"][k])
    for section in ("contribution_pct", "pct_study_var"):
        for k, v in ref_metrics[section].items():
            _compare_scalar(f"{section}.{k}", v, m[section][k])
    _compare_scalar("规格占比", ref_metrics["pct_tolerance_grr"],
                    m["pct_tolerance_grr"])
    _compare_scalar("ndc", ref_metrics["ndc"], m["ndc"])

    means = result["means"]
    _compare_scalar("总均值", ref["means"]["grand"], means["grand"]["mean"])
    for g in means["by_part"]:
        _compare_scalar(f"零件均值.{g['part_id']}",
                        ref["means"]["by_part"][g["part_id"]], g["mean"])
    for g in means["by_operator"]:
        _compare_scalar(f"操作员均值.{g['operator_id']}",
                        ref["means"]["by_operator"][g["operator_id"]], g["mean"])
    for g in means["cells"]:
        _compare_scalar(f"格均值.{g['part_id']}|{g['operator_id']}",
                        ref["means"]["cells"][(g["part_id"], g["operator_id"])],
                        g["mean"])

    # 原始行定位：逐行比对换算后的读数
    ref_rows = sorted(rows, key=lambda r: (r["part_id"], r["operator_id"],
                                           r["repeat_id"]))
    got_rows = result["observations"]
    check("观测行数一致", len(ref_rows) == len(got_rows),
          f"{len(ref_rows)} vs {len(got_rows)}")
    for exp, got in zip(ref_rows, got_rows):
        same = (exp["row"] == got["row"]
                and exp["part_id"] == got["part_id"]
                and exp["operator_id"] == got["operator_id"]
                and exp["repeat_id"] == got["repeat_id"]
                and _close(exp["canonical_value"], got["canonical_value"]))
        check(f"行定位 row={exp['row']}", same,
              "" if same else f"参考 {exp} vs 结果 {got}")


def _result_path(store: str, calc_id: str) -> Path:
    return Path(store) / "gage_rr" / f"{calc_id}.json"


def cmd_verify_compute(args: argparse.Namespace) -> int:
    result = json.loads(_result_path(args.store, args.calc_id)
                        .read_text(encoding="utf-8"))
    if result["status"] not in ("OK", "OK_PARTIAL"):
        print(f"calc {args.calc_id} 状态为 {result['status']}，"
              f"仅复核 OK/OK_PARTIAL 结果")
        return 2
    inp = result["input"]
    study, parts, operators, repeats, canonical, spec, cells, rows = _load(
        Path(inp["study"]), Path(inp["observations"]))
    print(f"== 独立重算 {args.calc_id}（试验 {study['study_id']}，"
          f"{len(parts)} 零件 × {len(operators)} 人 × {repeats} 次，"
          f"量纲 {canonical}）==")
    ref = _ref_anova(parts, operators, repeats, cells, result["alpha"])
    ref_metrics = _ref_metrics(ref["used"], spec)
    _compare_result(result, ref, ref_metrics, rows)
    return _summary()


def _summary() -> int:
    if FAILURES:
        print(f"\n复核失败 {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\n全部复核通过")
    return 0


# --------------------------------------------------------------------------- #
# 不变性核对（对生产入口的性质检验：等价输入必须给出等价统计结果）
# --------------------------------------------------------------------------- #
def _run_cli(store: str, study: Path, obs: Path, calc_id: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "-m", "mes_pipeline.cli",
         "--store", store, "grr-run",
         "--study", str(study), "--observations", str(obs),
         "--calc-id", calc_id],
        cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"grr-run 失败({proc.returncode}): {proc.stderr}")
    return json.loads(proc.stdout)


def _s1_variant_files(work: Path):
    study = json.loads((DEMO / "s1_interaction_kept" / "study.json")
                       .read_text(encoding="utf-8"))
    lines = (DEMO / "s1_interaction_kept" / "observations.csv") \
        .read_text(encoding="utf-8").splitlines()
    rows = [line.split(",") for line in lines[1:]]

    def emit(name: str, study_obj: dict, row_list: list[list[str]]):
        d = work / name
        d.mkdir(parents=True)
        (d / "study.json").write_text(json.dumps(study_obj, ensure_ascii=False),
                                      encoding="utf-8")
        body = "\n".join(["study_id,part_id,operator_id,repeat_id,value,unit"]
                         + [",".join(r) for r in row_list]) + "\n"
        (d / "observations.csv").write_text(body, encoding="utf-8")
        return d / "study.json", d / "observations.csv"

    variants = {"base": emit("base", study, rows)}

    ren = {"P1": "P3", "P3": "P1", "OP-A": "OP-B", "OP-B": "OP-A"}
    s2 = dict(study)
    s2["parts"] = [ren.get(p, p) for p in study["parts"]]
    s2["operators"] = [ren.get(o, o) for o in study["operators"]]
    variants["renamed"] = emit("renamed", s2,
                               [[r[0], ren.get(r[1], r[1]), ren.get(r[2], r[2]),
                                 r[3], r[4], r[5]] for r in rows])

    import random
    shuffled = [list(r) for r in rows]
    random.Random(20261113).shuffle(shuffled)
    variants["shuffled"] = emit("shuffled", dict(study), shuffled)

    s3 = json.loads(json.dumps(study))
    s3["spec"]["lower"] += 0.5
    s3["spec"]["upper"] += 0.5
    variants["shifted"] = emit(
        "shifted", s3,
        [[r[0], r[1], r[2], r[3], f"{float(r[4]) + 0.5:.2f}", r[5]]
         for r in rows])

    s4 = json.loads(json.dumps(study))
    s4["unit"] = "μm"
    s4["spec"]["lower"] *= 1000.0
    s4["spec"]["upper"] *= 1000.0
    variants["um"] = emit(
        "um", s4,
        [[r[0], r[1], r[2], r[3], f"{float(r[4]) * 1000.0:g}", "μm"]
         for r in rows])
    return variants


def _stat_sections(r: dict) -> dict:
    return {k: r[k] for k in ("anova", "variance_components", "metrics")}


def _flatten(node, prefix=""):
    out = {}
    if isinstance(node, dict):
        for k, v in node.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = node
    return out


def _check_equal_stats(name: str, got: dict, expect: dict) -> None:
    g, e = _flatten(got), _flatten(expect)
    # abs_diff 是 SS 分解核对的浮点噪声诊断量，随量纲/平移变化，不属于统计结果
    ignore = {"anova.ss_decomposition_check.abs_diff"}
    g = {k: v for k, v in g.items() if k not in ignore}
    e = {k: v for k, v in e.items() if k not in ignore}
    check(f"{name}: 键集合一致", set(g) == set(e))
    for key in sorted(set(g) & set(e)):
        x, y = g[key], e[key]
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) \
                and not isinstance(x, bool):
            check(f"{name}: {key}", _close(float(x), float(y)),
                  "" if _close(float(x), float(y)) else f"{x} vs {y}")
        else:
            check(f"{name}: {key}", x == y, "" if x == y else f"{x!r} vs {y!r}")


def cmd_verify_invariance(args: argparse.Namespace) -> int:
    print("== 不变性核对（对 s1 施加等价变换后经生产 CLI 重算）==")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        variants = _s1_variant_files(work)
        results = {}
        for name, (sp, op) in variants.items():
            results[name] = _run_cli(str(work / "store"), sp, op,
                                     f"inv-{name}")
        base = _stat_sections(results["base"])

        _check_equal_stats("交换零件/人员命名", _stat_sections(results["renamed"]),
                           base)
        _check_equal_stats("打乱行序", _stat_sections(results["shuffled"]), base)
        _check_equal_stats("整体平移 +0.5", _stat_sections(results["shifted"]),
                           base)

        # mm → μm：方差 ×1e6、标准差 ×1e3、比例类不变
        um = results["um"]
        scaled = json.loads(json.dumps(base))  # 深拷贝
        for term in ("part", "operator", "interaction", "error", "total"):
            scaled["anova"]["terms"][term]["ss"] *= 1e6
            if "ms" in scaled["anova"]["terms"][term]:
                scaled["anova"]["terms"][term]["ms"] *= 1e6
        scaled["anova"]["ss_decomposition_check"]["ss_total"] *= 1e6
        scaled["anova"]["ss_decomposition_check"]["ss_sum_of_terms"] *= 1e6
        for section in ("raw", "used"):
            for k, v in scaled["variance_components"][section].items():
                if v is not None:
                    scaled["variance_components"][section][k] = v * 1e6
        for k in scaled["metrics"]["variance"]:
            scaled["metrics"]["variance"][k] *= 1e6
        for k in scaled["metrics"]["stddev"]:
            scaled["metrics"]["stddev"][k] *= 1e3
        for k in scaled["metrics"]["study_var_6sigma"]:
            scaled["metrics"]["study_var_6sigma"][k] *= 1e3
        _check_equal_stats("mm→μm 换算(方差×1e6/标准差×1e3/比例不变)",
                           _stat_sections(um), scaled)
    return _summary()


def cmd_verify_all(args: argparse.Namespace) -> int:
    ids = ["demo-s1", "demo-s2", "demo-s3", "demo-s4", "demo-s5-fixed"]
    for calc_id in ids:
        ns = argparse.Namespace(store=args.store, calc_id=calc_id)
        rc = cmd_verify_compute(ns)
        if rc != 0:
            return rc
    return cmd_verify_invariance(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("verify-compute", help="独立重算并与保存结果逐项核对")
    p.add_argument("--store", required=True)
    p.add_argument("--calc-id", required=True)
    p.set_defaults(func=cmd_verify_compute)

    p = sub.add_parser("verify-invariance", help="等价变换核对（调生产 CLI 重算）")
    p.add_argument("--store", required=True)
    p.set_defaults(func=cmd_verify_invariance)

    p = sub.add_parser("verify-all", help="全部复核")
    p.add_argument("--store", required=True)
    p.set_defaults(func=cmd_verify_all)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
