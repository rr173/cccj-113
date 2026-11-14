"""devicehll 命令行：build / merge / estimate（纯标准库，可在 -I 下运行）。

退出码：0 成功 / 4 输入不合法 / 5 摘要格式错误 / 6 参数或兼容性错误 / 7 估计失败。
"""
from __future__ import annotations

import argparse
import json
import sys

from .errors import (
    SketchCompatibilityError,
    SketchEstimationError,
    SketchFormatError,
    SketchIOError,
)
from .service import build_sketch, estimate_file, merge_sketch_files


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fail(status: str, exc: Exception, code: int) -> int:
    _print({"status": status, "error": str(exc)})
    return code


def cmd_build(args: argparse.Namespace) -> int:
    try:
        _print(build_sketch(args.input, args.field, args.p, args.seed, args.out))
        return 0
    except SketchIOError as exc:
        return _fail("INVALID_INPUT", exc, 4)
    except SketchCompatibilityError as exc:
        return _fail("INCOMPATIBLE", exc, 6)
    except OSError as exc:
        return _fail("INVALID_INPUT", exc, 4)


def cmd_merge(args: argparse.Namespace) -> int:
    try:
        _print(merge_sketch_files(args.inputs, args.q, args.out))
        return 0
    except SketchFormatError as exc:
        return _fail("FORMAT_ERROR", exc, 5)
    except SketchCompatibilityError as exc:
        return _fail("INCOMPATIBLE", exc, 6)
    except OSError as exc:
        return _fail("INVALID_INPUT", exc, 4)


def cmd_estimate(args: argparse.Namespace) -> int:
    try:
        _print(estimate_file(args.file))
        return 0
    except SketchFormatError as exc:
        return _fail("FORMAT_ERROR", exc, 5)
    except SketchEstimationError as exc:
        return _fail("ESTIMATION_FAILED", exc, 7)
    except OSError as exc:
        return _fail("INVALID_INPUT", exc, 4)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mes_pipeline.devicehll",
        description="设备标识基数摘要（HyperLogLog）：流式构建、合并、估计")
    sub = parser.add_subparsers(dest="command", required=True)

    p_b = sub.add_parser("build", help="流式构建一个集合摘要")
    p_b.add_argument("--input", required=True, help="逐行 JSON 文件")
    p_b.add_argument("--field", required=True, help="设备标识字段名")
    p_b.add_argument("--p", type=int, required=True, help="精度 p（4..18，m=2^p）")
    p_b.add_argument("--seed", required=True, help="非空文本 seed")
    p_b.add_argument("--out", required=True, help="输出摘要文件路径")
    p_b.set_defaults(func=cmd_build)

    p_m = sub.add_parser("merge", help="并集合并多个摘要到目标精度 q")
    p_m.add_argument("--inputs", nargs="+", required=True, help="输入摘要文件")
    p_m.add_argument("--q", type=int, required=True, help="目标精度 q（≤ 任一输入 p）")
    p_m.add_argument("--out", required=True, help="输出摘要文件路径")
    p_m.set_defaults(func=cmd_merge)

    p_e = sub.add_parser("estimate", help="读取摘要并输出估计说明")
    p_e.add_argument("--file", required=True, help="摘要文件路径")
    p_e.set_defaults(func=cmd_estimate)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
