"""HyperLogLog 核心：哈希、rank、精确/稠密两种表示、降精度折叠与并集合并。

哈希约定（device-hll-v1）
-------------------------
对 seed 与标识先编码为 UTF-8，依次连接：

    b"device-hll-v1\\0" ‖ len(seed)（4 字节大端）‖ seed ‖ len(ident)（4 字节大端）‖ ident

取 SHA-256 摘要的前 8 字节按大端解释为 64 位无符号整数 x。
seed 不是密钥，只为得到可复现的哈希空间；不得用 Python 自带 hash()。

寄存器下标与 rank
-----------------
x 的高 p 位为下标 j；剩余低 64−p 位从高位起数连续零的个数再加 1 为 rank；
余位全零时 rank = 65 − p（该表示下的最大值）。更新取 max。

精确模式
--------
不同标识数 ≤ floor(m/4) 时保留原始字符串集合（受 256 字节上限约束），
计数精确；超过门槛把全部字符串喂入寄存器并释放集合，此后保持稠密。

降精度折叠
----------
把 p 位下标折到 q（q ≤ p）：原下标 j 的高 q 位是新下标，低 p−q 位 b 进入
rank 部分。b ≠ 0 时新 rank 只由 b 决定（b 的前导零 + 1）；b = 0 时新 rank
= (p−q) + 原 rank。空寄存器（0）不贡献。该规则与「同一批标识直接以 q
构建」逐寄存器相同——证明见 devicehll_demo/README.md。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set

from .errors import SketchCompatibilityError, SketchEstimationError

P_MIN = 4
P_MAX = 18
MAX_IDENT_BYTES = 256
HASH_PREFIX = b"device-hll-v1\0"
HASH_VERSION = 1
TWO64 = 2.0 ** 64

MODE_EXACT = "exact"
MODE_DENSE = "dense"


def validate_precision(p: int) -> int:
    if isinstance(p, bool) or not isinstance(p, int):
        raise SketchCompatibilityError(f"精度必须是整数，得到 {p!r}")
    if not (P_MIN <= p <= P_MAX):
        raise SketchCompatibilityError(f"精度 p 必须在 {P_MIN}..{P_MAX} 之间，得到 {p}")
    return p


def validate_seed(seed: str) -> str:
    if not isinstance(seed, str):
        raise SketchCompatibilityError(f"seed 必须是文本，得到 {type(seed).__name__}")
    if seed == "":
        raise SketchCompatibilityError("seed 不能为空文本")
    try:
        seed.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SketchCompatibilityError(f"seed 不是可 UTF-8 编码的文本: {exc}") from exc
    return seed


def seed_bytes(seed: str) -> bytes:
    return seed.encode("utf-8")


def hash64(seed_b: bytes, ident_b: bytes) -> int:
    """按 device-hll-v1 约定计算 64 位哈希。"""
    h = hashlib.sha256()
    h.update(HASH_PREFIX)
    h.update(len(seed_b).to_bytes(4, "big"))
    h.update(seed_b)
    h.update(len(ident_b).to_bytes(4, "big"))
    h.update(ident_b)
    return int.from_bytes(h.digest()[:8], "big")


def index_of(x: int, p: int) -> int:
    """高 p 位为寄存器下标。"""
    return x >> (64 - p)


def rank_of(x: int, p: int) -> int:
    """剩余 64−p 位的前导零 + 1；余位全零为 65−p。"""
    w = x & ((1 << (64 - p)) - 1)
    if w == 0:
        return 65 - p
    return (64 - p) - w.bit_length() + 1


def alpha_for(m: int) -> float:
    if m == 16:
        return 0.673
    if m == 32:
        return 0.697
    if m == 64:
        return 0.709
    return 0.7213 / (1.0 + 1.079 / m)


def fold_registers(registers: bytes, p_from: int, p_to: int) -> bytearray:
    """把 p_from 精度的寄存器折到 p_to（p_to ≤ p_from）。

    空寄存器（0）不贡献虚构观测；其余按「高 q 位作新下标，低 p−q 位进
    入 rank」的规则折算，结果与同一批标识直接以 p_to 构建逐寄存器相同。
    """
    if not (P_MIN <= p_to <= p_from <= P_MAX):
        raise SketchCompatibilityError(
            f"折叠要求 {P_MIN} ≤ p_to ≤ p_from ≤ {P_MAX}，得到 p_from={p_from}, p_to={p_to}")
    out = bytearray(1 << p_to)
    if p_to == p_from:
        out[:] = registers
        return out
    shift = p_from - p_to
    mask = (1 << shift) - 1
    for j, r in enumerate(registers):
        if r == 0:
            continue
        b = j & mask
        if b:
            rank = shift - b.bit_length() + 1
        else:
            rank = shift + r
        j_to = j >> shift
        if rank > out[j_to]:
            out[j_to] = rank
    return out


@dataclass(frozen=True)
class Estimate:
    mode: str                 # "exact" | "dense"
    p: int
    correction: str           # "none" | "raw" | "linear_counting" | "large_range_64"
    value: float              # exact 时为整数（以 float 表示）
    empty_registers: Optional[int]

    @property
    def relative_standard_error(self) -> Optional[float]:
        if self.mode != MODE_DENSE:
            return None
        return 1.04 / math.sqrt(1 << self.p)


class Sketch:
    """一个集合摘要：精确模式（字符串集合）或稠密模式（寄存器数组）。"""

    def __init__(self, p: int, seed: str):
        self.p = validate_precision(p)
        self.seed = validate_seed(seed)
        self.hash_version = HASH_VERSION
        self._seed_b = seed_bytes(seed)
        self._exact: Optional[Set[str]] = set()
        self._registers: Optional[bytearray] = None

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    @classmethod
    def from_exact(cls, p: int, seed: str, items: Iterable[str]) -> "Sketch":
        sk = cls(p, seed)
        items = list(items)
        if len(items) > sk.exact_threshold:
            raise SketchCompatibilityError(
                f"精确模式标识数 {len(items)} 超过 floor(m/4)={sk.exact_threshold} 门槛")
        seen: Set[str] = set()
        for s in items:
            if not isinstance(s, str):
                raise SketchCompatibilityError(f"精确标识必须是字符串，得到 {type(s).__name__}")
            b = s.encode("utf-8")
            if len(b) > MAX_IDENT_BYTES:
                raise SketchCompatibilityError(f"精确标识超过 {MAX_IDENT_BYTES} 字节上限")
            if s in seen:
                raise SketchCompatibilityError(f"重复精确标识 {s!r}")
            seen.add(s)
        sk._exact = seen
        return sk

    @classmethod
    def from_dense(cls, p: int, seed: str, registers: bytes) -> "Sketch":
        sk = cls(p, seed)
        m = 1 << p
        if len(registers) != m:
            raise SketchCompatibilityError(
                f"稠密寄存器数 {len(registers)} 与 m={m} 不符")
        max_rank = 65 - p
        for i, r in enumerate(registers):
            if r > max_rank:
                raise SketchCompatibilityError(
                    f"非法寄存器值：寄存器 {i} 为 {r}，超过上限 {max_rank}")
        sk._exact = None
        sk._registers = bytearray(registers)
        return sk

    # ------------------------------------------------------------------ #
    # 状态访问
    # ------------------------------------------------------------------ #
    @property
    def mode(self) -> str:
        return MODE_EXACT if self._exact is not None else MODE_DENSE

    @property
    def exact_threshold(self) -> int:
        return (1 << self.p) // 4

    @property
    def exact_items(self) -> Optional[Set[str]]:
        return None if self._exact is None else set(self._exact)

    @property
    def registers(self) -> Optional[bytes]:
        return None if self._registers is None else bytes(self._registers)

    def registers_at_own_p(self) -> bytes:
        """返回本摘要自身精度下的寄存器（精确模式则现场喂入字符串）。"""
        if self._registers is not None:
            return bytes(self._registers)
        regs = bytearray(1 << self.p)
        for s in sorted(self._exact, key=lambda x: x.encode("utf-8")):
            x = hash64(self._seed_b, s.encode("utf-8"))
            j = index_of(x, self.p)
            r = rank_of(x, self.p)
            if r > regs[j]:
                regs[j] = r
        return bytes(regs)

    # ------------------------------------------------------------------ #
    # 更新
    # ------------------------------------------------------------------ #
    def add(self, ident: str) -> None:
        b = ident.encode("utf-8")
        if len(b) > MAX_IDENT_BYTES:
            raise ValueError(f"标识超过 {MAX_IDENT_BYTES} 字节上限")
        if self._exact is not None:
            self._exact.add(ident)
            if len(self._exact) > self.exact_threshold:
                self._convert_to_dense()
        else:
            self._add_dense(ident)

    def _add_dense(self, ident: str) -> None:
        x = hash64(self._seed_b, ident.encode("utf-8"))
        j = index_of(x, self.p)
        r = rank_of(x, self.p)
        if r > self._registers[j]:
            self._registers[j] = r

    def _convert_to_dense(self) -> None:
        regs = bytearray(1 << self.p)
        exact, self._exact = self._exact, None
        self._registers = regs
        for s in exact:
            self._add_dense(s)

    def _to_dense_empty(self) -> None:
        self._exact = None
        self._registers = bytearray(1 << self.p)

    # ------------------------------------------------------------------ #
    # 估计
    # ------------------------------------------------------------------ #
    def estimate(self) -> Estimate:
        if self._exact is not None:
            return Estimate(mode=MODE_EXACT, p=self.p, correction="none",
                            value=float(len(self._exact)), empty_registers=None)
        m = 1 << self.p
        alpha = alpha_for(m)
        z = math.fsum(2.0 ** (-r) for r in self._registers)
        raw = alpha * m * m / z
        empty = self._registers.count(0)
        if raw <= 2.5 * m and empty:
            return Estimate(mode=MODE_DENSE, p=self.p, correction="linear_counting",
                            value=m * math.log(m / empty), empty_registers=empty)
        if raw > TWO64 / 30.0:
            if raw >= TWO64:
                raise SketchEstimationError(
                    "原始估计超过 2^64，64 位大范围修正无定义，明确失败")
            return Estimate(mode=MODE_DENSE, p=self.p, correction="large_range_64",
                            value=-TWO64 * math.log1p(-raw / TWO64),
                            empty_registers=empty)
        return Estimate(mode=MODE_DENSE, p=self.p, correction="raw",
                        value=raw, empty_registers=empty)


def merge_sketches(sketches: Sequence[Sketch], q: int) -> Sketch:
    """并集合并到目标精度 q（q ≤ 任一输入精度）。

    - seed 与哈希版本必须一致；
    - 禁止把低精度补零冒充高精度（q > 任一输入 p 即拒绝）；
    - 任一来源已稠密则结果稠密；全精确且并集 ≤ floor(m_q/4) 则保持精确；
    - 稠密路径：各来源先在自身精度下得到寄存器，再折叠到 q 后取 max。
    """
    if not sketches:
        raise SketchCompatibilityError("至少需要一个输入摘要")
    q = validate_precision(q)
    first = sketches[0]
    for s in sketches:
        if s.hash_version != first.hash_version:
            raise SketchCompatibilityError(
                f"哈希版本不一致：{s.hash_version} 与 {first.hash_version}")
        if s.seed != first.seed:
            raise SketchCompatibilityError("seed 不一致，禁止合并")
        if q > s.p:
            raise SketchCompatibilityError(
                f"目标精度 q={q} 超过输入精度 p={s.p}：禁止把低精度补零冒充高精度")
    out = Sketch(q, first.seed)
    m_q = 1 << q
    if all(s.mode == MODE_EXACT for s in sketches):
        union: Set[str] = set()
        for s in sketches:
            union.update(s._exact)
        if len(union) <= m_q // 4:
            out._exact = union
            return out
    out._to_dense_empty()
    for s in sketches:
        folded = fold_registers(s.registers_at_own_p(), s.p, q)
        for i, r in enumerate(folded):
            if r > out._registers[i]:
                out._registers[i] = r
    return out
