"""逐行 JSON 设备上报的流式读取。

每行一条 JSON 对象，指定字段为设备标识：必须存在，值只接受字符串或
null；null 不参与集合，空字符串是有效标识。标识按解码后的原文本识别，
不裁空格、不改大小写、不把 007 当成 7；UTF-8 编码超过 256 字节或坏行
（非法 JSON/非对象/缺字段/类型不符/重复键）给出行号并整体失败。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Optional, Tuple

from .core import MAX_IDENT_BYTES
from .errors import SketchIOError


def _no_duplicate_keys(pairs):
    seen = set()
    out = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"同一 JSON 对象内重复键 {key!r}")
        seen.add(key)
        out[key] = value
    return out


def iter_identifiers(path: str | Path, field: str) -> Iterator[Tuple[int, Optional[str]]]:
    """逐行产出 (行号, 标识或 None)；坏行抛 :class:`SketchIOError`（含行号）。

    空白行跳过（不计为记录）；行号从 1 开始（含空白行计数）。
    """
    label = Path(path).name
    try:
        fh = open(path, "rb")
    except OSError as exc:
        raise SketchIOError([f"{label}: 文件不可读: {exc}"]) from exc
    with fh:
        for line_no, raw in enumerate(fh, start=1):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SketchIOError(
                    [f"{label} 第{line_no}行: 不是合法 UTF-8: {exc}"]) from exc
            if not line.strip():
                continue
            try:
                obj = json.loads(
                    line,
                    object_pairs_hook=_no_duplicate_keys,
                    parse_constant=lambda c: (_ for _ in ()).throw(
                        ValueError(f"非法 JSON 常量 {c}")),
                )
            except json.JSONDecodeError as exc:
                raise SketchIOError(
                    [f"{label} 第{line_no}行: JSON 解析失败（第{exc.colno}列）: {exc.msg}"]) from exc
            except ValueError as exc:
                raise SketchIOError([f"{label} 第{line_no}行: {exc}"]) from exc
            if not isinstance(obj, dict):
                raise SketchIOError([f"{label} 第{line_no}行: 记录必须是 JSON 对象"])
            if field not in obj:
                raise SketchIOError(
                    [f"{label} 第{line_no}行: 缺少标识字段 {field!r}"])
            value = obj[field]
            if value is None:
                yield line_no, None
                continue
            if not isinstance(value, str):
                raise SketchIOError(
                    [f"{label} 第{line_no}行: 标识字段 {field!r} 的值类型为 "
                     f"{type(value).__name__}（只允许字符串或 null）"])
            try:
                b = value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise SketchIOError(
                    [f"{label} 第{line_no}行: 标识不是可 UTF-8 编码的文本: {exc}"]) from exc
            if len(b) > MAX_IDENT_BYTES:
                raise SketchIOError(
                    [f"{label} 第{line_no}行: 标识 UTF-8 编码 {len(b)} 字节，"
                     f"超过 {MAX_IDENT_BYTES} 上限"])
            yield line_no, value
