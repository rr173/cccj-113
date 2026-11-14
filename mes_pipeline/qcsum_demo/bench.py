"""规模基准：十万批次、百万样本装载 + 两类小更改的增量应用与核对。

用法：
    python -m qcsum_demo.bench --work /tmp/qcsum_bench \
        --lots 100000 --samples 1000000 --seed 42

流程：
1. 确定性生成材料并 load_initial 装载（唯一一次全量扫描），记录耗时与内存；
2. 批次 A：挑一个只有少量样本的批次，换供应商（跨组）并同批删/加样本；
3. 批次 B：把 3 条样本跨组转移到另一组的批次；
4. 每批打印运行统计（受影响批次、读取的关联样本数、调整的辅助键数、耗时），
   随后用完整 LEFT JOIN/GROUP BY 参考查询独立核对（参考耗时单独列出）；
5. 报告峰值内存（tracemalloc 与 ru_maxrss）。

增量路径只触及本批键及其样本邻域：从统计可见两次小更改没有扫描其余百万行。
"""
from __future__ import annotations

import argparse
import json
import resource
import time
import tracemalloc
from pathlib import Path

from mes_pipeline.qcsum.datagen import (
    build_batch_cross_transfer,
    build_batch_move_small_lot,
    generate_lots,
    generate_samples,
)
from mes_pipeline.qcsum.reference import diff_summary, reference_summary
from mes_pipeline.qcsum.store import SummaryStore


def _rss_kib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _check(store, tag):
    """独立参考查询核对（计时单列；参考查询只在核对中使用）。"""
    t0 = time.perf_counter()
    expected = reference_summary(store.conn)
    ref_ms = (time.perf_counter() - t0) * 1000
    diffs = diff_summary(expected, store.summary())
    if diffs:
        raise AssertionError(f"{tag}: 持久汇总与参考查询不一致: {diffs[:5]}")
    return {"groups": len(expected), "reference_ms": round(ref_ms, 3)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="质检汇总规模基准")
    parser.add_argument("--work", default="/tmp/qcsum_bench")
    parser.add_argument("--lots", type=int, default=100_000)
    parser.add_argument("--samples", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    db = work / "qcsum_bench.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    report = {"lots": args.lots, "samples": args.samples, "seed": args.seed}
    tracemalloc.start()

    store = SummaryStore(db)
    t0 = time.perf_counter()
    load_result = store.load_initial(
        generate_lots(args.lots, seed=args.seed),
        generate_samples(args.samples, args.lots, seed=args.seed))
    report["load"] = load_result
    _, peak = tracemalloc.get_traced_memory()
    report["load"]["tracemalloc_peak_mib"] = round(peak / 2**20, 2)

    report["verify_after_load"] = _check(store, "装载后")

    # 批次 A：小样本批跨组 + 同批删/加样本
    batch_a = build_batch_move_small_lot(store.conn, "BENCH-A")
    (work / "batch_a.json").write_text(
        json.dumps(batch_a, ensure_ascii=False, indent=2), encoding="utf-8")
    tracemalloc.reset_peak()
    result_a = store.apply_batch(batch_a)
    _, peak_a = tracemalloc.get_traced_memory()
    report["batch_a"] = {"file": "batch_a.json", "result": result_a,
                         "tracemalloc_peak_kib": round(peak_a / 2**10, 1)}
    report["verify_after_a"] = _check(store, "批次 A 后")

    # 批次 B：3 条样本跨组转移
    batch_b = build_batch_cross_transfer(store.conn, "BENCH-B")
    (work / "batch_b.json").write_text(
        json.dumps(batch_b, ensure_ascii=False, indent=2), encoding="utf-8")
    tracemalloc.reset_peak()
    result_b = store.apply_batch(batch_b)
    _, peak_b = tracemalloc.get_traced_memory()
    report["batch_b"] = {"file": "batch_b.json", "result": result_b,
                         "tracemalloc_peak_kib": round(peak_b / 2**10, 1)}
    report["verify_after_b"] = _check(store, "批次 B 后")

    report["ru_maxrss_mib"] = round(_rss_kib() / 2**10, 2)
    store.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
