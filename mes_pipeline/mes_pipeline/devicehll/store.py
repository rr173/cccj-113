"""devicehll 摘要的版本化磁盘格式（DEVHLL1）与原子写入。

布局（全部大端）::

    0   6 字节  魔数 b"DEVHLL"
    6   2 字节  格式版本 uint16 = 1
    8   1 字节  p（4..18）
    9   1 字节  模式（0=exact, 1=dense）
    10  2 字节  保留，必须为 0
    12  4 字节  seed 字节长度 N（≥1）
    16  N 字节  seed UTF-8
    --  载荷 --
    末 32 字节  SHA-256（以上全部字节）

载荷（exact）：uint32 计数 K，随后 K 条（uint32 长度 L + L 字节 UTF-8），
按 UTF-8 字节序排列；K ≤ floor(m/4)，L ≤ 256，不得重复。
载荷（dense）：m = 2^p 字节，每字节一个寄存器，值 ≤ 65−p。

校验顺序：固定头 → seed 区 → 载荷结构走查（只信长度前缀）→ 校验和 →
语义校验（重复标识/非法寄存器/超门槛/超长标识）。这样截断、多余尾字节、
校验和损坏、语义非法各自可归因。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import List, Tuple

from .core import (
    HASH_VERSION,
    MAX_IDENT_BYTES,
    MODE_DENSE,
    MODE_EXACT,
    P_MAX,
    P_MIN,
    Sketch,
)
from .errors import SketchCompatibilityError, SketchFormatError

MAGIC = b"DEVHLL"
FORMAT_VERSION = 1
_HEADER_LEN = 16
_CHECKSUM_LEN = 32
_MODE_TO_CODE = {MODE_EXACT: 0, MODE_DENSE: 1}
_CODE_TO_MODE = {0: MODE_EXACT, 1: MODE_DENSE}


def encode(sketch: Sketch) -> bytes:
    """把摘要序列化为 DEVHLL1 字节串。"""
    seed_b = sketch.seed.encode("utf-8")
    header = (
        MAGIC
        + FORMAT_VERSION.to_bytes(2, "big")
        + bytes([sketch.p, _MODE_TO_CODE[sketch.mode]])
        + b"\x00\x00"
        + len(seed_b).to_bytes(4, "big")
        + seed_b
    )
    if sketch.mode == MODE_EXACT:
        items = sorted(sketch.exact_items, key=lambda s: s.encode("utf-8"))
        payload = len(items).to_bytes(4, "big") + b"".join(
            len(s.encode("utf-8")).to_bytes(4, "big") + s.encode("utf-8")
            for s in items
        )
    else:
        payload = bytes(sketch.registers)
    body = header + payload
    return body + hashlib.sha256(body).digest()


def decode(raw: bytes) -> Sketch:
    """解析并校验 DEVHLL1 字节串；任何问题抛 :class:`SketchFormatError`。"""
    if len(raw) < _HEADER_LEN + _CHECKSUM_LEN:
        raise SketchFormatError("截断：文件不足固定头与校验和长度")
    if raw[:6] != MAGIC:
        raise SketchFormatError("魔数不符：不是 DEVHLL 摘要文件")
    version = int.from_bytes(raw[6:8], "big")
    if version != FORMAT_VERSION:
        raise SketchFormatError(f"未知版本 {version}（本实现只支持 {FORMAT_VERSION}）")
    p = raw[8]
    mode_code = raw[9]
    reserved = raw[10:12]
    if not (P_MIN <= p <= P_MAX):
        raise SketchFormatError(f"非法精度 p={p}（允许 {P_MIN}..{P_MAX}）")
    if mode_code not in _CODE_TO_MODE:
        raise SketchFormatError(f"非法模式码 {mode_code}")
    if reserved != b"\x00\x00":
        raise SketchFormatError("保留字节非零")
    seed_len = int.from_bytes(raw[12:16], "big")
    if seed_len < 1:
        raise SketchFormatError("seed 为空：格式要求非空文本 seed")
    if _HEADER_LEN + seed_len + _CHECKSUM_LEN > len(raw):
        raise SketchFormatError("截断：seed 区或校验和不完整")
    seed_b = raw[16:16 + seed_len]
    payload = raw[16 + seed_len: len(raw) - _CHECKSUM_LEN]

    mode = _CODE_TO_MODE[mode_code]
    spans: List[Tuple[int, int]] = []
    k = 0
    if mode == MODE_EXACT:
        if len(payload) < 4:
            raise SketchFormatError("截断：精确模式缺少计数")
        k = int.from_bytes(payload[:4], "big")
        off = 4
        for _ in range(k):
            if off + 4 > len(payload):
                raise SketchFormatError("截断：精确标识长度前缀不完整")
            L = int.from_bytes(payload[off:off + 4], "big")
            off += 4
            if off + L > len(payload):
                raise SketchFormatError("截断：精确标识内容不完整")
            spans.append((off, L))
            off += L
        if off != len(payload):
            raise SketchFormatError(f"多余尾字节：载荷结束后仍有 {len(payload) - off} 字节")
    else:
        m = 1 << p
        if len(payload) < m:
            raise SketchFormatError("截断：寄存器区不完整")
        if len(payload) > m:
            raise SketchFormatError(f"多余尾字节：寄存器区后仍有 {len(payload) - m} 字节")

    expect = hashlib.sha256(raw[:-_CHECKSUM_LEN]).digest()
    if expect != raw[-_CHECKSUM_LEN:]:
        raise SketchFormatError("校验和损坏：SHA-256 不匹配")

    try:
        seed = seed_b.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SketchFormatError(f"seed 不是合法 UTF-8: {exc}") from exc

    if mode == MODE_EXACT:
        threshold = (1 << p) // 4
        if k > threshold:
            raise SketchFormatError(f"精确模式标识数 {k} 超过 floor(m/4)={threshold} 门槛")
        items = []
        seen = set()
        for off, L in spans:
            if L > MAX_IDENT_BYTES:
                raise SketchFormatError(f"精确标识 {L} 字节超过 {MAX_IDENT_BYTES} 上限")
            b = payload[off:off + L]
            try:
                s = b.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SketchFormatError(f"精确标识不是合法 UTF-8: {exc}") from exc
            if s in seen:
                raise SketchFormatError(f"重复精确标识 {s!r}")
            seen.add(s)
            items.append(s)
        return Sketch.from_exact(p, seed, items)

    regs = bytearray(payload)
    max_rank = 65 - p
    for i, r in enumerate(regs):
        if r > max_rank:
            raise SketchFormatError(f"非法寄存器值：寄存器 {i} 为 {r}，超过上限 {max_rank}")
    return Sketch.from_dense(p, seed, bytes(regs))


def load_file(path: str | Path) -> Sketch:
    """只读加载摘要文件。"""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise SketchFormatError(f"摘要文件不可读: {exc}") from exc
    return decode(raw)


def atomic_write(path: str | Path, data: bytes) -> None:
    """同目录临时文件 + fsync + os.replace；失败清理临时文件，保留旧字节。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
