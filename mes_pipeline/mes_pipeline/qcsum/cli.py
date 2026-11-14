"""质检汇总命令行（纯标准库，可在 -P/-I 隔离模式下运行）。

子命令：

- qcsum init DB                          初始化库（建模式，幂等）
- qcsum load DB --lots F --samples F     空库装载初始两表（.json 数组或 .ndjson）
- qcsum apply DB BATCH.json [--crash-at P]  应用一批更改（P ∈ after_base/after_commit）
- qcsum summary DB                       读取持久汇总（直接 SELECT，不重算）
- qcsum dump DB lots|samples             读取原表
- qcsum batches DB                       读取成功账
- qcsum verify DB                        用独立参考查询核对持久汇总与空组
- qcsum demo [--work DIR]                逐步演示八组场景并逐组核对

退出码：0 成功 / 4 输入不合法或状态不允许 / 5 批次号冲突 / 1 核对不一致。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import QcsumConflict, QcsumError
from .reference import diff_summary, reference_summary
from .scenarios import scenarios
from .store import CRASH_POINTS, SUMMARY_COLUMNS, SummaryStore
from .validate import loads_strict


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _read_rows(path):
    """读取 .json（数组）或 .ndjson（每行一个对象）；ndjson 走流式迭代。"""
    path = Path(path)
    if str(path).endswith((".ndjson", ".jsonl")):
        def _iter():
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        yield loads_strict(line)
        return _iter()
    data = loads_strict(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise QcsumError(f"{path}: 顶层必须是 JSON 数组（或使用 .ndjson）")
    return data


def cmd_init(args):
    with SummaryStore(args.db):
        pass
    _print({"status": "OK", "db": args.db})
    return 0


def cmd_load(args):
    lots = _read_rows(args.lots)
    samples = _read_rows(args.samples)
    with SummaryStore(args.db) as store:
        result = store.load_initial(lots, samples)
    _print(result)
    return 0


def cmd_apply(args):
    text = Path(args.batch).read_text(encoding="utf-8")
    with SummaryStore(args.db) as store:
        result = store.apply_batch(text, crash_at=args.crash_at)
    _print(result)
    return 0


def cmd_summary(args):
    with SummaryStore(args.db) as store:
        _print(store.summary())
    return 0


def cmd_dump(args):
    with SummaryStore(args.db) as store:
        _print(store.base_rows(args.table))
    return 0


def cmd_batches(args):
    with SummaryStore(args.db) as store:
        _print(store.batches())
    return 0


def cmd_verify(args):
    """独立核对：持久汇总 vs 完整 LEFT JOIN/GROUP BY 参考查询。"""
    with SummaryStore(args.db) as store:
        actual = store.summary()
        expected = reference_summary(store.conn)
    diffs = diff_summary(expected, actual)
    if diffs:
        _print({"status": "MISMATCH", "diffs": diffs})
        return 1
    _print({"status": "OK", "groups": len(actual),
            "note": "持久汇总与独立参考查询一致（含空组情形）"})
    return 0


def _print_summary_table(rows):
    header = ("supplier", "site", "joined", "samples", "lots", "grades", "rej_sum")
    print("    %-10s %-8s %7s %8s %5s %7s %s" % header)
    if not rows:
        print("    （空：没有任何分组）")
    for r in rows:
        rej = "NULL" if r["rejected_sum"] is None else str(r["rejected_sum"])
        print("    %-10s %-8s %7d %8d %5d %7d %s" % (
            r["supplier"], r["site"], r["joined_rows"], r["n_samples"],
            r["n_lots"], r["n_grades"], rej))


def cmd_demo(args):
    """逐步演示八组场景；每步应用一批后与独立参考查询核对。"""
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    failures = 0
    for sc_id, sc_title, steps in scenarios():
        db = work / f"qcsum_demo_{sc_id}.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        print(f"[{sc_id}] {sc_title}")
        with SummaryStore(db) as store:
            for step_title, batch in steps:
                result = store.apply_batch(batch)
                rows = store.summary()
                expected = reference_summary(store.conn)
                diffs = diff_summary(expected, rows)
                print(f"  步骤：{step_title}")
                print(f"  批次 {batch['batch_id']}：{len(batch['ops'])} 个操作，"
                      f"统计 {json.dumps(result['stats'], ensure_ascii=False)}")
                _print_summary_table(rows)
                if diffs:
                    failures += 1
                    print(f"  参考查询核对: 不一致 {diffs}")
                else:
                    print("  参考查询核对: OK")
        print()
    if failures:
        print(f"演示结束：{failures} 步不一致")
        return 1
    print("演示结束：全部步骤与独立参考查询一致")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="qcsum", description="质检仓库增量关系汇总（SQLite 持久化）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化库（建模式，幂等）")
    p.add_argument("db")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("load", help="空库装载初始两表（.json 数组或 .ndjson）")
    p.add_argument("db")
    p.add_argument("--lots", required=True)
    p.add_argument("--samples", required=True)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("apply", help="应用一批更改（JSON 文件）")
    p.add_argument("db")
    p.add_argument("batch")
    p.add_argument("--crash-at", choices=CRASH_POINTS, default=None,
                   help="可重复故障注入点（仅故障演练）")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("summary", help="读取持久汇总")
    p.add_argument("db")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("dump", help="读取原表")
    p.add_argument("db")
    p.add_argument("table", choices=("lots", "samples"))
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("batches", help="读取成功账")
    p.add_argument("db")
    p.set_defaults(func=cmd_batches)

    p = sub.add_parser("verify", help="用独立参考查询核对持久汇总")
    p.add_argument("db")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("demo", help="逐步演示八组场景并逐组核对")
    p.add_argument("--work", default="/tmp/qcsum_demo")
    p.set_defaults(func=cmd_demo)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except QcsumConflict as exc:
        _print({"status": "CONFLICT", "error": str(exc)})
        return 5
    except QcsumError as exc:
        _print({"status": "REJECTED", "error": str(exc)})
        return 4


if __name__ == "__main__":
    sys.exit(main())
