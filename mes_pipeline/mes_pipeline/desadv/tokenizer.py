"""字节级流式切分器：有界块读取、UNA 识别与释放符感知切段。

设计约束（对应接入约定）：

- 输入按有界字节块读取（``block_size`` 可调），任何时刻内存中只保留
  「当前块缓冲 + 当前段」，整文件不 read/split；
- 全程在 **字节** 上识别服务字符。UTF-8 多字节序列的续字节最高位为 1，
  不可能与 ASCII 服务字符冲突，因此中文字符跨块只是普通的缓冲拼接；
- 只有 **未被释放的** 段结束符/元素分隔符/组件分隔符才参与切分；
  连续分隔符与末尾空元素原样保留；
- 释放符让紧跟的一个服务字符（含释放符自身）成为普通数据；
  对其他字符的无意义释放、文件末尾悬空释放一律报错；
- 段起止以原文件字节偏移（左闭右开，含段结束符）记录，可直接切回原文；
- UNA、释放序列与段结束符本身也可能跨块，由缓冲窗口拼接处理。

缓冲模型
--------
``_buf`` 始终是一个字节窗口，``_abs`` 为 ``_buf[0]`` 的文件绝对偏移；
``_cursor`` 为当前消费位置的文件绝对偏移（满足 ``_abs <= _cursor``）。
窗口只在需要查看的字节超出窗口右端时按有界块扩展，并在消费位置推进后
丢弃前缀，保证内存不随文件增长。
"""
from __future__ import annotations

import hashlib
import string
from dataclasses import dataclass
from typing import BinaryIO, Callable, List, Optional

from .errors import DesadvError

#: 未出现 UNA 时的默认服务字符（UNA:+.? '）
DEFAULT_UNA = b"UNA:+.? '"
_ASCII_PUNCT = frozenset(string.punctuation.encode("ascii"))
_TERMINATORS = b"\r\n"


@dataclass(frozen=True)
class ServiceChars:
    """UNA 六个位置解析后的服务字符（均为单字节 int）。"""

    component: int       # 位置1：组件分隔符
    element: int         # 位置2：元素分隔符
    decimal: int         # 位置3：小数符（只允许 . 或 ,）
    release: int         # 位置4：释放（转义）符
    reserve: int         # 位置5：保留位，必须为空格
    terminator: int      # 位置6：段结束符

    def as_una_bytes(self) -> bytes:
        return b"UNA" + bytes((
            self.component, self.element, self.decimal, self.release,
            self.reserve, self.terminator,
        ))


def default_service_chars() -> ServiceChars:
    return _parse_una(DEFAULT_UNA, 0)


def _parse_una(una: bytes, base_offset: int) -> ServiceChars:
    """解析并校验完整的 9 字节 UNA 段。"""
    if len(una) != 9 or una[:3] != b"UNA":
        raise DesadvError("UNA 段必须恰好为 UNA 加六个服务字符共 9 字节",
                          byte_offset=base_offset)
    comp, elem, decimal, release, reserve, term = una[3:9]
    if reserve != 0x20:
        raise DesadvError("UNA 第五位（保留空格位）必须为空格",
                          byte_offset=base_offset + 7)
    if decimal not in (0x2E, 0x2C):  # '.' 或 ','
        raise DesadvError("UNA 小数符只允许 '.' 或 ','",
                          byte_offset=base_offset + 5)
    four = (comp, elem, release, term)
    for index, value in zip((3, 4, 6, 8), four):
        if value not in _ASCII_PUNCT:
            raise DesadvError("UNA 服务字符必须是 ASCII 标点",
                              byte_offset=base_offset + index)
        if value == decimal:
            raise DesadvError("UNA 服务字符不能与小数符相同",
                              byte_offset=base_offset + index)
    if len(set(four)) != 4:
        raise DesadvError("UNA 四个服务字符必须互不相同",
                          byte_offset=base_offset + 3)
    return ServiceChars(comp, elem, decimal, release, reserve, term)


@dataclass
class Segment:
    """切分并解码后的一个段（保留原始字节与字节偏移）。"""

    tag: str
    elements: List[List[str]]      # elements[i][j]：第 i 元素第 j 组件（已解码）
    raw: bytes                     # 含段结束符的原始字节
    start: int                     # 段起始字节偏移
    end: int                       # 段结束字节偏移（不含；含段结束符）

    @property
    def span(self) -> tuple[int, int]:
        return self.start, self.end


class SegmentStreamer:
    """有界块字节流上的交换头/段读取器。

    Parameters
    ----------
    read:
        类文件对象的 ``read(n)``（二进制）。
    block_size:
        每次读取的有界字节块大小，必须 >= 1。
    max_segment_bytes:
        单段长度上限（含段结束符），超出即拒绝，避免无界内存占用。
    """

    def __init__(self, read: Callable[[int], bytes], block_size: int = 65536,
                 max_segment_bytes: int = 1_048_576) -> None:
        if block_size < 1:
            raise ValueError("block_size 必须 >= 1")
        if max_segment_bytes < 1:
            raise ValueError("max_segment_bytes 必须 >= 1")
        self._read = read
        self.block_size = block_size
        self.max_segment_bytes = max_segment_bytes
        self._buf = bytearray()
        self._abs = 0                 # _buf[0] 的文件绝对偏移
        self._cursor = 0              # 当前消费位置的文件绝对偏移
        self._eof = False
        self.chars: Optional[ServiceChars] = None
        self._hasher = None

    # ------------------------------------------------------------------ #
    # 窗口与哈希
    # ------------------------------------------------------------------ #
    @property
    def file_offset(self) -> int:
        return self._cursor

    def _discard_prefix(self) -> None:
        """消费位置推进后丢弃窗口前缀，保证内存有界。"""
        if self._cursor > self._abs:
            consumed = self._cursor - self._abs
            del self._buf[:consumed]
            self._abs = self._cursor

    def _extend(self) -> bool:
        """读一个有界块扩展窗口右端；返回窗口内是否还有字节。"""
        if self._eof:
            return self._cursor < self._abs + len(self._buf)
        chunk = self._read(self.block_size)
        if not chunk:
            self._eof = True
        else:
            self._buf.extend(chunk)
        return self._cursor < self._abs + len(self._buf)

    def _byte_at(self, offset: int) -> Optional[int]:
        """取文件绝对偏移 offset 处的字节；超出窗口则按块扩展，EOF 返回 None。"""
        while offset >= self._abs + len(self._buf):
            if self._eof:
                return None
            chunk = self._read(self.block_size)
            if not chunk:
                self._eof = True
                return None
            self._buf.extend(chunk)
        return self._buf[offset - self._abs]

    def _advance(self, n: int) -> bytes:
        """消费 [_cursor, _cursor+n)，同步哈希并推进游标。"""
        # 保证窗口覆盖整个区间
        while self._cursor + n > self._abs + len(self._buf):
            if self._eof:
                raise DesadvError("读取越过文件结尾（内部错误）",
                                  byte_offset=self._cursor)
            chunk = self._read(self.block_size)
            if not chunk:
                self._eof = True
                raise DesadvError("读取越过文件结尾（文件截断）",
                                  byte_offset=self._cursor)
            self._buf.extend(chunk)
        out = bytes(self._buf[self._cursor - self._abs:
                              self._cursor - self._abs + n])
        if self._hasher is not None:
            self._hasher.update(out)
        self._cursor += n
        self._discard_prefix()
        return out

    def begin_hashing(self) -> None:
        self._hasher = hashlib.sha256()

    def finish_hashing(self) -> str:
        if self._hasher is None:
            raise RuntimeError("未开始哈希")
        digest = self._hasher.hexdigest()
        self._hasher = None
        return digest

    # ------------------------------------------------------------------ #
    # 交换开头
    # ------------------------------------------------------------------ #
    def begin_exchange(self) -> Optional[tuple[ServiceChars,
                                               Optional[tuple[int, int]]]]:
        """定位下一个交换开头，返回（服务字符, UNA 跨度或 None）；EOF 返回 None。"""
        while True:
            b = self._byte_at(self._cursor)
            if b is None:
                return None
            if b in _TERMINATORS:
                self._advance(1)        # 交换之间的 CR/LF 不属于任何交换
                continue
            break
        exchange_start = self._cursor
        self.begin_hashing()            # 交换哈希从 UNA（或 UNB）起算
        if self._byte_at(exchange_start) == ord("U") \
                and self._byte_at(exchange_start + 1) == ord("N") \
                and self._byte_at(exchange_start + 2) == ord("A"):
            # 凑齐 9 字节（UNA 可跨块）
            for off in range(3, 9):
                if self._byte_at(exchange_start + off) is None:
                    raise DesadvError("UNA 段不完整（文件截断）",
                                      byte_offset=exchange_start)
            una_bytes = self._advance(9)
            chars = _parse_una(una_bytes, exchange_start)
            self.chars = chars
            return chars, (exchange_start, exchange_start + 9)
        self.chars = default_service_chars()
        return self.chars, None

    # ------------------------------------------------------------------ #
    # 段读取
    # ------------------------------------------------------------------ #
    def next_segment(self) -> Optional[Segment]:
        """读取下一个完整段；交换内遇到文件结束返回 None。"""
        assert self.chars is not None, "需先 begin_exchange"
        chars = self.chars
        # 段之间允许 CR/LF（属于交换字节，参与交换哈希）
        while True:
            b = self._byte_at(self._cursor)
            if b is None:
                return None
            if b not in _TERMINATORS:
                break
            self._advance(1)
        start = self._cursor
        scan = start
        length = 0
        while True:
            b = self._byte_at(scan)
            if b is None:
                raise DesadvError("段缺少段结束符（文件截断）",
                                  byte_offset=start)
            if b == chars.release:
                nxt = self._byte_at(scan + 1)
                if nxt is None:
                    raise DesadvError("释放符后文件结束（悬空释放）",
                                      byte_offset=scan)
                scan += 2
                length += 2
            elif b == chars.terminator:
                scan += 1
                length += 1
                break
            else:
                scan += 1
                length += 1
            if length > self.max_segment_bytes:
                raise DesadvError(
                    f"单段长度超过上限 {self.max_segment_bytes} 字节",
                    byte_offset=start)
        raw = self._advance(length)
        end = self._cursor
        return self._build_segment(raw, start, end, chars)

    def _build_segment(self, raw: bytes, start: int, end: int,
                       chars: ServiceChars) -> Segment:
        content = raw[:-1]
        elements = self._split(content, chars, start)
        if not elements or not elements[0] or not elements[0][0]:
            raise DesadvError("段标签缺失或为空", byte_offset=start,
                              end_byte_offset=end)
        if len(elements[0]) != 1:
            raise DesadvError("段标签元素不允许含组件分隔符",
                              byte_offset=start, end_byte_offset=end)
        return Segment(tag=elements[0][0], elements=elements, raw=raw,
                       start=start, end=end)

    def _split(self, content: bytes, chars: ServiceChars,
               base: int) -> List[List[str]]:
        """释放符感知地切元素/组件，并完成释放合法性与 UTF-8 校验。"""
        elements: List[List[bytearray]] = [[bytearray()]]
        i = 0
        n = len(content)
        service = {chars.component, chars.element, chars.release,
                   chars.terminator}
        while i < n:
            b = content[i]
            if b == chars.release:
                if i + 1 >= n:
                    raise DesadvError("释放符后缺少被释放字符（悬空释放）",
                                      byte_offset=base + i)
                nxt = content[i + 1]
                if nxt not in service:
                    raise DesadvError(
                        "释放符只能转义服务字符（组件/元素/段结束/释放符自身），"
                        "不接受无意义释放",
                        byte_offset=base + i)
                elements[-1][-1].append(nxt)
                i += 2
                continue
            if b == chars.element:
                elements.append([bytearray()])
            elif b == chars.component:
                elements[-1].append(bytearray())
            else:
                elements[-1][-1].append(b)
            i += 1
        decoded: List[List[str]] = []
        for comps in elements:
            dec_comps: List[str] = []
            for comp in comps:
                try:
                    dec_comps.append(bytes(comp).decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise DesadvError(
                        f"字段不是合法 UTF-8：{exc.reason}",
                        byte_offset=base,
                        end_byte_offset=base + n + 1) from exc
            decoded.append(dec_comps)
        return decoded


def stream_file(handle: BinaryIO, block_size: int = 65536,
                max_segment_bytes: int = 1_048_576) -> SegmentStreamer:
    """便捷构造：绑定一个二进制文件句柄。"""
    return SegmentStreamer(handle.read, block_size, max_segment_bytes)
