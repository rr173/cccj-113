"""EDIFACT DESADV 接入器命令行（纯标准库，可在 -I 隔离模式下运行）。

子命令：

- desadv-import   流式导入文件到 SQLite（可调块大小/段长上限/故障注入）
- desadv-list     列出交换与通知
- desadv-line     查询一条发货行（根→包装路径、数量、备注、来源偏移）
- desadv-subtree  任意包装的子树数量汇总（按零件号+单位）
- desadv-slice    用库存字节偏移从原文件切回原始段并回显
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import DesadvConflict, DesadvError
from .queries import get_line, list_exchanges, list_messages, subtree_summary, verify_slice
from .service import import_file


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_import(args: argparse.Namespace) -> int:
    try:
        result = import_file(
            args.db, args.file, block_size=args.block_size,
            max_segment_bytes=args.max_segment_bytes,
            fault_inject_before_commit=args.fault_inject_before_commit)
    except DesadvConflict as exc:
        _print({"status": "CONFLICT", "error": str(exc)})
        return 5
    except DesadvError as exc:
        _print({"status": "REJECTED", "error": str(exc)})
        return 4
    payload = {
        "status": "OK",
        "db": result.db_path,
        "source_file": result.source_file,
        "block_size": result.block_size,
        "exchange_count": len(result.exchanges),
        "total_line_count": result.total_lines,
        "exchanges": [vars(e) for e in result.exchanges],
    }
    _print(payload)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    payload = {
        "exchanges": list_exchanges(args.db),
        "messages": list_messages(args.db),
    }
    _print(payload)
    return 0


def cmd_line(args: argparse.Namespace) -> int:
    try:
        _print(get_line(args.db, args.message_no, args.line_no,
                        args.source_file))
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def cmd_subtree(args: argparse.Namespace) -> int:
    try:
        _print(subtree_summary(args.db, args.message_no, args.package))
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def cmd_slice(args: argparse.Namespace) -> int:
    raw = verify_slice(args.db, args.file, args.start, args.end)
    sys.stdout.buffer.write(raw)
    if not raw.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mes_pipeline.desadv",
        description="EDIFACT DESADV 发货通知接入器（子集，UTF-8）")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_imp = sub.add_parser("desadv-import", help="流式导入 DESADV 文件")
    p_imp.add_argument("--file", required=True, help="原始 EDI 文件（UTF-8）")
    p_imp.add_argument("--block-size", type=int, default=65536,
                       help="有界读取块大小（字节，默认 65536；验收可用 1/2/7）")
    p_imp.add_argument("--max-segment-bytes", type=int, default=1_048_576,
                       help="单段长度上限（字节，默认 1048576）")
    p_imp.add_argument("--fault-inject-before-commit", action="store_true",
                       help="故障注入：首个交换校验通过、COMMIT 前以退出码 11 退出")
    p_imp.set_defaults(func=cmd_import)

    p_list = sub.add_parser("desadv-list", help="列出库内交换与通知")
    p_list.set_defaults(func=cmd_list)

    p_line = sub.add_parser("desadv-line", help="查询发货行（路径/数量/备注）")
    p_line.add_argument("--message-no", required=True)
    p_line.add_argument("--line-no", required=True)
    p_line.add_argument("--source-file", default=None,
                        help="可选：按来源文件消歧")
    p_line.set_defaults(func=cmd_line)

    p_sub = sub.add_parser("desadv-subtree", help="包装子树数量汇总")
    p_sub.add_argument("--message-no", required=True)
    p_sub.add_argument("--package", required=True)
    p_sub.set_defaults(func=cmd_subtree)

    p_slice = sub.add_parser("desadv-slice",
                             help="按字节偏移从原文件切回原始段")
    p_slice.add_argument("--file", required=True)
    p_slice.add_argument("--start", type=int, required=True)
    p_slice.add_argument("--end", type=int, required=True)
    p_slice.set_defaults(func=cmd_slice)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
