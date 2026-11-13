"""实耗批次谱系结果的独立复核脚本（不调用 mes_pipeline.lineage 的任何函数）。

复核路径与生产代码完全独立：自行解析两份输入 JSON 与已保存的结果 JSON，
用 :class:`fractions.Fraction` 独立实现拓扑排序、均匀混合传播、损耗分配、
余额与全局账，再逐项核对生产结果中的原料贡献、余额、损耗和召回总量。

子命令：
- ``verify-diamond``  对含菱形路径的 s3 小数据做完整手工数值复核；
- ``verify-all``      依次复核全部演示场景（含拒绝场景的退出状态）。

用法（仓库根目录）：
    python3 -I lineage_demo/verify.py verify-all --store /tmp/line_store
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = Path(__file__).resolve().parent

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# --------------------------------------------------------------------------- #
# 独立解析（不复用项目 io 模块）
# --------------------------------------------------------------------------- #
def _qty(node: dict) -> Fraction:
    unit = node["unit"].strip().lower()
    value = Fraction(str(node["mass"]))
    return value * 1000 if unit == "kg" else value


def _load_inputs(materials_path: Path, operations_path: Path):
    mats = json.loads(materials_path.read_text(encoding="utf-8"))
    ops_doc = json.loads(operations_path.read_text(encoding="utf-8"))
    initial = {b["batch_id"]: _qty(b["initial_mass"]) for b in mats["batches"]}
    operations = []
    for op in ops_doc["operations"]:
        operations.append({
            "operation_id": op["operation_id"],
            "inputs": [(x["batch_id"], _qty(x)) for x in op["inputs"]],
            "outputs": [(x["batch_id"], _qty(x)) for x in op["outputs"]],
            "loss": _qty(op["loss_mass"]),
        })
    return initial, operations


# --------------------------------------------------------------------------- #
# 独立参考传播（Fraction 精确算术，均匀混合假设）
# --------------------------------------------------------------------------- #
def reference_lineage(initial: dict, operations: list):
    producer = {}
    for op in operations:
        for bid, _ in op["outputs"]:
            producer[bid] = op["operation_id"]

    # Kahn 拓扑
    ids = {op["operation_id"] for op in operations}
    indeg = {k: 0 for k in ids}
    adj = {k: [] for k in ids}
    for op in operations:
        for bid, _ in op["inputs"]:
            if bid in producer and producer[bid] != op["operation_id"]:
                adj[producer[bid]].append(op["operation_id"])
                indeg[op["operation_id"]] += 1
    ready = sorted(k for k, d in indeg.items() if d == 0)
    order = []
    by_id = {op["operation_id"]: op for op in operations}
    while ready:
        cur = ready.pop(0)
        order.append(by_id[cur])
        for nxt in sorted(adj[cur]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort()

    # composition[bid] = {raw_id: 质量 g}（满批生成组成）
    composition = {bid: {bid: mass} for bid, mass in initial.items()}
    full_mass = dict(initial)
    consumed = {bid: Fraction(0) for bid in initial}
    loss_by_raw: dict[str, Fraction] = {}

    for op in order:
        total_in = sum((m for _, m in op["inputs"]), Fraction(0))
        pooled: dict[str, Fraction] = {}
        for bid, qty in op["inputs"]:
            scale = qty / full_mass[bid]
            consumed[bid] = consumed.get(bid, Fraction(0)) + qty
            for raw, mass in composition[bid].items():
                pooled[raw] = pooled.get(raw, Fraction(0)) + mass * scale
        assert sum(pooled.values(), Fraction(0)) == total_in
        for bid, qty in op["outputs"]:
            s = qty / total_in
            composition[bid] = {r: m * s for r, m in pooled.items()}
            full_mass[bid] = qty
            consumed.setdefault(bid, Fraction(0))
        if op["loss"] > 0:
            s = op["loss"] / total_in
            for r, m in pooled.items():
                loss_by_raw[r] = loss_by_raw.get(r, Fraction(0)) + m * s

    remaining = {bid: full_mass[bid] - consumed.get(bid, Fraction(0))
                 for bid in full_mass}
    remain_composition = {
        bid: {r: m * (remaining[bid] / full_mass[bid])
              for r, m in composition[bid].items()}
        for bid in composition
    }
    return {
        "full_mass": full_mass,
        "remaining": remaining,
        "composition": composition,
        "remain_composition": remain_composition,
        "loss_by_raw": loss_by_raw,
        "consumed": consumed,
    }


def _f(text: str) -> Fraction:
    return Fraction(text)


# --------------------------------------------------------------------------- #
# s3 菱形：手工数值逐项核对
# --------------------------------------------------------------------------- #
EXPECTED_S3 = {
    # 批: (生成质量, 余额, {原料: 生成构成}, {原料: 余额构成})
    "RM-X": (10000, 0, {"RM-X": 10000}, {"RM-X": 0}),
    "RM-Y": (6000, 0, {"RM-Y": 6000}, {"RM-Y": 0}),
    "M-1": (16000, 2000,
            {"RM-X": 10000, "RM-Y": 6000},
            {"RM-X": 1250, "RM-Y": 750}),
    "D-1": (4000, 0,
            {"RM-X": 2500, "RM-Y": 1500},
            {"RM-X": 0, "RM-Y": 0}),
    "D-2": (3000, 0,
            {"RM-X": 1875, "RM-Y": 1125},
            {"RM-X": 0, "RM-Y": 0}),
    "F-1": (7000, 7000,
            {"RM-X": 4375, "RM-Y": 2625},
            {"RM-X": 4375, "RM-Y": 2625}),
}
EXPECTED_LOSS = {"RM-X": 4375, "RM-Y": 2625}
EXPECTED_LEDGER = {
    "RM-X": (10000, 5625, 4375),   # 初始, 剩余贡献合计, 损耗贡献
    "RM-Y": (6000, 3375, 2625),
}


def verify_diamond(store: str) -> None:
    print("== s3 菱形路径独立复核（Fraction 精确核算） ==")
    scenario = DEMO / "s3_diamond"
    initial, operations = _load_inputs(
        scenario / "materials.json", scenario / "operations_reverse.json")
    ref = reference_lineage(initial, operations)

    # 1) 手工期望值 vs 独立参考实现
    for bid, (prod, rem, gen, remc) in EXPECTED_S3.items():
        check(f"{bid} 生成质量={prod}g",
              ref["full_mass"][bid] == Fraction(prod),
              str(ref["full_mass"][bid]))
        check(f"{bid} 余额={rem}g",
              ref["remaining"][bid] == Fraction(rem), str(ref["remaining"][bid]))
        for raw, want in gen.items():
            check(f"{bid} 生成构成 {raw}={want}g",
                  ref["composition"][bid][raw] == Fraction(want),
                  str(ref["composition"][bid][raw]))
        for raw, want in remc.items():
            check(f"{bid} 余额构成 {raw}={want}g",
                  ref["remain_composition"][bid][raw] == Fraction(want),
                  str(ref["remain_composition"][bid][raw]))

    # 2) 菱形两支相加、不去重不双算
    f1_x = ref["composition"]["F-1"]["RM-X"]
    check("F-1 的 RM-X = 两支相加 2500+1875",
          f1_x == Fraction(4375) == Fraction(2500) + Fraction(1875), str(f1_x))

    # 3) 损耗
    for raw, want in EXPECTED_LOSS.items():
        check(f"损耗 {raw}={want}g",
              ref["loss_by_raw"][raw] == Fraction(want),
              str(ref["loss_by_raw"][raw]))

    # 4) 全局账
    for raw, (init, rem_sum, loss_sum) in EXPECTED_LEDGER.items():
        actual_rem = sum(
            (c.get(raw, Fraction(0)) for c in ref["remain_composition"].values()),
            Fraction(0))
        check(f"全局账 {raw}: {init} = 剩余 {rem_sum} + 损耗 {loss_sum}",
              Fraction(init) == actual_rem + ref["loss_by_raw"][raw]
              and actual_rem == Fraction(rem_sum),
              f"剩余实际 {actual_rem}")

    # 5) 与生产结果 JSON 逐项比对
    saved = json.loads(
        (Path(store) / "lineage_calcs" / "demo-s3.json").read_text(encoding="utf-8"))
    saved_batches = {b["batch_id"]: b for b in saved["batches"]}
    for bid, (prod, rem, gen, remc) in EXPECTED_S3.items():
        sb = saved_batches[bid]
        check(f"结果文件 {bid} 生成质量",
              _f(sb["produced_mass_g"]) == Fraction(prod))
        check(f"结果文件 {bid} 余额",
              _f(sb["remaining_mass_g"]) == Fraction(rem))
        gen_saved = {c["raw_batch_id"]: _f(c["mass_g"])
                     for c in sb["generated_composition"]}
        rem_saved = {c["raw_batch_id"]: _f(c["mass_g"])
                     for c in sb["remaining_composition"]}
        for raw, want in gen.items():
            check(f"结果文件 {bid} 生成构成 {raw}",
                  gen_saved[raw] == Fraction(want))
        for raw, want in remc.items():
            check(f"结果文件 {bid} 余额构成 {raw}",
                  rem_saved[raw] == Fraction(want))

    # 6) 召回总量（双嫌疑，同批只隔离一次；耗尽中间批不计）
    recall = json.loads(_run_cli(store, "lineage-recall",
                                 "--calc-id", "demo-s3",
                                 "--suspect", "RM-X", "--suspect", "RM-Y").stdout)
    check("双嫌疑召回总量=9000g（F-1 7000 + M-1 余 2000）",
          _f(recall["total_quarantine_mass_g"]) == Fraction(9000),
          recall["total_quarantine_mass_g"])
    ids = {it["batch_id"] for it in recall["items"]}
    check("耗尽中间批 D-1/D-2 不隔离", "D-1" not in ids and "D-2" not in ids, str(ids))
    check("F-1 只隔离一次",
          len([it for it in recall["items"] if it["batch_id"] == "F-1"]) == 1)
    for entry in recall["suspect_ledgers"]:
        check(f"召回全局账 {entry['raw_batch_id']} 平衡", entry["balanced"])

    # 7) 单嫌疑召回
    recall_x = json.loads(_run_cli(store, "lineage-recall",
                                   "--calc-id", "demo-s3",
                                   "--suspect", "RM-X").stdout)
    total_x = sum((_f(it["available_mass_g"]) for it in recall_x["items"]),
                  Fraction(0))
    check("单嫌疑 RM-X 召回总量=9000g（两批都含 X）",
          total_x == Fraction(9000), str(total_x))


def _run_cli(store: str, *args: str) -> subprocess.CompletedProcess:
    # -I 隔离模式下 cwd/PYTHONPATH 不进 sys.path；显式注入仓库根目录，
    # editable 安装环境下去掉该注入也等价。
    bootstrap = (
        "import runpy, sys; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        f"sys.argv = ['mes_pipeline.cli', '--store', {store!r}] + {list(args)!r}; "
        "runpy.run_module('mes_pipeline.cli', run_name='__main__')"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", bootstrap],
        cwd=ROOT, capture_output=True, text=True)


def verify_other_scenarios(store: str) -> None:
    print("== 其余演示场景独立复核 ==")
    cases = [
        ("s1_mix_partial", "operations.json", {
            "RM-A": (10000, 4000), "RM-B": (5000, 3000), "M-1": (8000, 8000)}),
        ("s2_split_loss", "operations.json", {
            "RM-C": (10000, 0), "P-1": (3000, 3000), "P-2": (2500, 2500)}),
        ("s4_exact_exhaust", "operations.json", {
            "RM-P": (4000, 0), "RM-Q": (1000, 0),
            "E-1": (3000, 3000), "E-2": (2000, 2000)}),
    ]
    for name, ops_file, wants in cases:
        d = DEMO / name
        initial, operations = _load_inputs(d / "materials.json", d / ops_file)
        ref = reference_lineage(initial, operations)
        for bid, (prod, rem) in wants.items():
            check(f"{name}/{bid} 生成={prod} 余额={rem}",
                  ref["full_mass"][bid] == Fraction(prod)
                  and ref["remaining"][bid] == Fraction(rem),
                  f"{ref['full_mass'][bid]}/{ref['remaining'][bid]}")
        # 每个原料全局账平衡
        for raw in initial:
            rem_sum = sum(
                (c.get(raw, Fraction(0))
                 for c in ref["remain_composition"].values()),
                Fraction(0))
            loss = ref["loss_by_raw"].get(raw, Fraction(0))
            check(f"{name} 全局账 {raw}",
                  initial[raw] == rem_sum + loss,
                  f"{initial[raw]} vs {rem_sum}+{loss}")

    print("== 拒绝场景退出码复核 ==")
    s5 = DEMO / "s5_reject_then_fix"
    for variant, expect_rc in [
        ("operations_overdraw_1g.json", 4),
        ("operations_unbalanced.json", 4),
        ("operations_missing_ref.json", 4),
        ("operations_cycle.json", 4),
    ]:
        proc = _run_cli(store, "lineage-load",
                        "--materials", str(s5 / "materials.json"),
                        "--operations", str(s5 / variant),
                        "--calc-id", f"verify-{variant.removesuffix('.json')}")
        check(f"{variant} 拒绝退出码 {expect_rc}", proc.returncode == expect_rc,
              f"实际 {proc.returncode}")
    fixed = _run_cli(store, "lineage-load",
                     "--materials", str(s5 / "materials.json"),
                     "--operations", str(s5 / "operations_fixed_reverse.json"),
                     "--calc-id", "verify-fixed")
    check("修正后倒序记录装载成功", fixed.returncode == 0,
          f"实际 {fixed.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description="谱系结果独立复核")
    parser.add_argument("command", choices=["verify-diamond", "verify-all"])
    parser.add_argument("--store", required=True)
    args = parser.parse_args()

    # 确保生产结果已存在（verify-diamond 依赖 demo-s3）
    if not (Path(args.store) / "lineage_calcs" / "demo-s3.json").exists():
        d = DEMO / "s3_diamond"
        _run_cli(args.store, "lineage-load",
                 "--materials", str(d / "materials.json"),
                 "--operations", str(d / "operations_reverse.json"),
                 "--calc-id", "demo-s3")

    verify_diamond(args.store)
    if args.command == "verify-all":
        verify_other_scenarios(args.store)

    print()
    if FAILURES:
        print(f"复核失败 {len(FAILURES)} 项:")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("全部复核通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
