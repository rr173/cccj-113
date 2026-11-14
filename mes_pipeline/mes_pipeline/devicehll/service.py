"""devicehll 编排层：流式构建、合并、估计三个入口。

所有命令行都通过这里进入核心。构建/合并写到既有目标时，只有所有输入
处理成功才整体替换（同目录临时文件 + os.replace）；失败保留目标原字节
并清理临时文件。对合并输入只读处理，命令结束后另一进程可只靠摘要文件
继续查询或合并。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from .core import (
    HASH_VERSION,
    MODE_DENSE,
    MODE_EXACT,
    Sketch,
    merge_sketches,
    validate_precision,
    validate_seed,
)
from .errors import SketchCompatibilityError
from .io import iter_identifiers
from .store import atomic_write, encode, load_file

STATUS_OK = "OK"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sketch_public(sk: Sketch) -> dict:
    payload = {
        "p": sk.p,
        "m": 1 << sk.p,
        "seed": sk.seed,
        "hash_version": sk.hash_version,
        "mode": sk.mode,
    }
    if sk.mode == MODE_EXACT:
        payload["distinct_exact"] = len(sk.exact_items)
    return payload


def build_sketch(input_path: str | Path, field: str, p: int, seed: str,
                 out_path: str | Path) -> dict:
    """流式构建一个集合摘要并原子写入 ``out_path``。"""
    validate_precision(p)
    validate_seed(seed)
    if not isinstance(field, str) or field == "":
        raise SketchCompatibilityError("标识字段名必须是非空文本")

    sk = Sketch(p, seed)
    lines_total = 0
    records = 0
    null_identifiers = 0
    identifiers_added = 0
    for line_no, ident in iter_identifiers(input_path, field):
        lines_total = line_no
        records += 1
        if ident is None:
            null_identifiers += 1
            continue
        sk.add(ident)
        identifiers_added += 1

    data = encode(sk)
    atomic_write(out_path, data)
    return {
        "status": STATUS_OK,
        "tool": "devicehll.build",
        "generated_at": _utc_now(),
        "input": {
            "file": str(input_path),
            "field": field,
            "sha256": _file_sha256(input_path),
        },
        "params": {"p": p, "m": 1 << p, "seed": seed, "hash_version": HASH_VERSION},
        "lines_total": lines_total,
        "records": records,
        "null_identifiers": null_identifiers,
        "identifiers_added": identifiers_added,
        "sketch": _sketch_public(sk),
        "out": {
            "file": str(out_path),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    }


def merge_sketch_files(input_paths: Sequence[str | Path], q: int,
                       out_path: str | Path) -> dict:
    """只读加载全部输入摘要，并集合并到目标精度 q 后原子写入 ``out_path``。"""
    if not input_paths:
        raise SketchCompatibilityError("至少需要一个输入摘要文件")
    sketches: List[Sketch] = []
    inputs_meta = []
    for path in input_paths:
        sk = load_file(path)  # 只读；任何格式/兼容问题在写出前抛出
        sketches.append(sk)
        inputs_meta.append({
            "file": str(path),
            "sha256": _file_sha256(path),
            **_sketch_public(sk),
        })
    merged = merge_sketches(sketches, q)
    data = encode(merged)
    atomic_write(out_path, data)
    return {
        "status": STATUS_OK,
        "tool": "devicehll.merge",
        "generated_at": _utc_now(),
        "inputs": inputs_meta,
        "params": {"q": q, "m": 1 << q, "seed": merged.seed,
                   "hash_version": merged.hash_version},
        "sketch": _sketch_public(merged),
        "out": {
            "file": str(out_path),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    }


def estimate_file(path: str | Path) -> dict:
    """只读加载摘要并输出估计说明（模式/修正/有效精度/估计值/误差尺度）。"""
    sk = load_file(path)
    est = sk.estimate()
    payload = {
        "status": STATUS_OK,
        "tool": "devicehll.estimate",
        "generated_at": _utc_now(),
        "file": str(path),
        "sha256": _file_sha256(path),
        "sketch": _sketch_public(sk),
        "estimate": est.value if sk.mode == MODE_DENSE else int(est.value),
        "exact": sk.mode == MODE_EXACT,
        "correction": est.correction,
        "empty_registers": est.empty_registers,
        "relative_standard_error": est.relative_standard_error,
        "rse_note": (
            "约 1.04/√m 的相对标准误差是估计量的统计涨落尺度（约 2/3 概率落在 "
            "±1 倍内），不是每份资料必然满足的误差上限；精确模式无此项。"
            if sk.mode == MODE_DENSE else
            "精确模式为真实去重计数，无统计误差。"
        ),
    }
    return payload
