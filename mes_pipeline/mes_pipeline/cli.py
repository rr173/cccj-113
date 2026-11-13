"""命令行入口：导入、封存、重算、轨迹查询。

示例
----
python -m mes_pipeline.cli seal     --config config.json --store ./store --package ./raw/pkg-a
python -m mes_pipeline.cli seal-set --config config.json --store ./store --set-id S1 \\
    --package pkg-a --package pkg-b
python -m mes_pipeline.cli recompute --config config.json --store ./store --set-id S1 --version v1
python -m mes_pipeline.cli query    --config config.json --store ./store --set-id S1 \\
    --batch SB-xxxx
python -m mes_pipeline.cli query    --config config.json --store ./store --set-id S1 \\
    --line LINE-A --label B-REWORK
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import PipelineConfig
from .pipeline import Pipeline


def _build(args: argparse.Namespace) -> Pipeline:
    config = PipelineConfig.load(args.config)
    return Pipeline(config, args.store)


def cmd_import(args: argparse.Namespace) -> int:
    pipe = _build(args)
    events, withdrawals, corrections = pipe.import_package(args.package)
    payload = {
        "event_count": len(events),
        "withdrawal_count": len(withdrawals),
        "correction_count": len(corrections),
        "first_events": [
            {
                "source_event_id": e.raw.source_event_id,
                "line_code": e.raw.line_code,
                "utc_time": e.utc_time.isoformat(),
                "wall_clock": e.raw.wall_clock,
            }
            for e in sorted(events, key=lambda x: x.sort_key)[:5]
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_seal(args: argparse.Namespace) -> int:
    pipe = _build(args)
    print(json.dumps(pipe.seal(args.package), ensure_ascii=False, indent=2))
    return 0


def cmd_seal_set(args: argparse.Namespace) -> int:
    pipe = _build(args)
    path = pipe.create_sealed_set(args.set_id, args.package)
    print(json.dumps({"sealed_set": args.set_id, "path": path}, ensure_ascii=False, indent=2))
    return 0


def cmd_recompute(args: argparse.Namespace) -> int:
    pipe = _build(args)
    out = pipe.recompute(args.set_id, version_id=args.version)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    pipe = _build(args)
    report = pipe.read_report(args.set_id, args.version)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    pipe = _build(args)
    if not args.batch and not (args.line and args.label):
        print("查询需提供 --batch 或 (--line 与 --label)", file=sys.stderr)
        return 2
    result = pipe.query_trace(
        args.set_id,
        synthetic_batch_id=args.batch,
        line_batch=(args.line, args.label) if args.line else None,
        version_id=args.version,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mes_pipeline", description="MES 流程分析管线")
    parser.add_argument("--config", required=True, help="管线配置 JSON（时钟 + 工艺规范）")
    parser.add_argument("--store", required=True, help="封存库目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p_imp = sub.add_parser("import", help="只读解析导出包")
    p_imp.add_argument("--package", required=True)
    p_imp.set_defaults(func=cmd_import)

    p_seal = sub.add_parser("seal", help="封存导出包（复制 + 哈希）")
    p_seal.add_argument("--package", required=True)
    p_seal.set_defaults(func=cmd_seal)

    p_set = sub.add_parser("seal-set", help="声明纳入分析的封存集合")
    p_set.add_argument("--set-id", required=True)
    p_set.add_argument("--package", action="append", required=True,
                       help="可重复传入多个 package_id")
    p_set.set_defaults(func=cmd_seal_set)

    p_rec = sub.add_parser("recompute", help="重算分析并版本化报告")
    p_rec.add_argument("--set-id", required=True)
    p_rec.add_argument("--version", default=None)
    p_rec.set_defaults(func=cmd_recompute)

    p_rep = sub.add_parser("report", help="读取已存报告")
    p_rep.add_argument("--set-id", required=True)
    p_rep.add_argument("--version", default=None)
    p_rep.set_defaults(func=cmd_report)

    p_q = sub.add_parser("query", help="轨迹查询")
    p_q.add_argument("--set-id", required=True)
    p_q.add_argument("--batch", default=None, help="合成批次 ID")
    p_q.add_argument("--line", default=None, help="产线代码")
    p_q.add_argument("--label", default=None, help="产线内 batch_label")
    p_q.add_argument("--version", default=None)
    p_q.set_defaults(func=cmd_query)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
