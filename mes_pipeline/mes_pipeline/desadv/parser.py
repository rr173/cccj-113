"""DESADV 约定子集的状态机解析与消息级校验。

只接受下列段与结构，未列出的段/结构一律拒绝：

- 交换：``UNB ... UNZ``（一个文件可串接多个交换，各带自己的 UNA）
- 消息：``UNH BGM DTM NAD NAD (CPS 包装组)* UNT``
- 包装组：``CPS PAC [GIN]? (LIN QTY FTX*)*``，无零件的包装允许存在

解析过程逐段把记录推给 ``ParseSink``（落库实现见 store.py），不在解析器里
积累整文件数据；消息内唯一性/层级校验只维护必要的小索引。所有数量以
``fractions.Fraction`` 精确表示，禁止 float 近似与指数写法。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Dict, List, Optional, Protocol

from .errors import DesadvError
from .tokenizer import Segment, ServiceChars

# 消息头约定
_MESSAGE_TYPE = ["DESADV", "D", "96A", "UN"]
_ALLOWED_UNITS = frozenset({"PCE", "KGM"})
_NAD_QUALIFIERS = frozenset({"SU", "BY"})
_POSITIVE_INT_RE = re.compile(r"^\d+$")
_DATE_RE = re.compile(r"^\d{8}$")
_UNB_DATE_RE = re.compile(r"^\d{6}$")
_UNB_TIME_RE = re.compile(r"^\d{4}$")

# 外层消息状态
_S_UNB, _S_UNH, _S_BGM, _S_DTM, _S_NAD, _S_BODY, _S_MSG_DONE = range(7)
# CPS 组内状态
_G_NONE, _G_AFTER_CPS, _G_AFTER_PAC, _G_AFTER_GIN, _G_AFTER_LINQTY = range(5)


# --------------------------------------------------------------------------- #
# 解析产物（值对象）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExchangeHeader:
    sender: str
    receiver: str
    date_ymd: str
    time_hhmm: str
    control: str
    start: int
    end: int
    has_una: bool
    una_start: Optional[int]
    una_end: Optional[int]
    decimal_char: str


@dataclass(frozen=True)
class MessageHeader:
    message_no: str
    doc_no: str
    doc_date: str
    su_code: str
    by_code: str


@dataclass(frozen=True)
class PackageRecord:
    package_id: str
    parent_id: Optional[str]          # None 表示根
    pieces: int
    package_type: str
    label: Optional[str]
    depth: int
    cps_start: int
    cps_end: int                      # 组结束位置（下一段起点；末组为 UNT 起点）


@dataclass(frozen=True)
class LineRecord:
    line_no: str
    part_no: str
    quantity: Fraction
    unit: str
    quantity_text: str                # 归一化（点小数）后的精确十进制原文
    package_id: str
    lin_start: int
    lin_end: int
    qty_start: int
    qty_end: int


@dataclass(frozen=True)
class NoteRecord:
    line_no: str
    seq: int                          # 所属行内的备注次序（从 1 开始）
    text: str
    start: int
    end: int


class ParseSink(Protocol):
    """解析器的落库端口；每个交换一个事务，由 service 层控制提交/回滚。"""

    def attach_exchange(self, header: ExchangeHeader) -> int:
        """登记当前交换头（交换行由 service 层在重传判定后统一插入）。"""
        ...

    def begin_message(self, exchange_id: int, seq_in_exchange: int,
                      header: MessageHeader) -> int: ...

    def update_message_header(self, message_id: int, *, doc_no: str,
                              doc_date: str, su_code: str, by_code: str,
                              partial: bool) -> None: ...

    def add_segment(self, exchange_id: int, message_id: Optional[int],
                    segment: Segment, seq_in_message: Optional[int],
                    seq_in_exchange: int) -> None: ...

    def add_package(self, message_id: int, record: PackageRecord) -> None: ...

    def upsert_package_fields(self, message_id: int, package_code: str,
                              **fields) -> None: ...

    def update_package_depth(self, message_id: int, package_code: str,
                             depth: int, cps_end: int) -> None: ...

    def add_line(self, message_id: int, record: LineRecord) -> None: ...

    def add_note(self, message_id: int, record: NoteRecord) -> None: ...


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _components(segment: Segment, elem_index: int) -> List[str]:
    return segment.elements[elem_index]


def _single(segment: Segment, elem_index: int, what: str) -> str:
    comps = _components(segment, elem_index)
    if len(comps) != 1:
        raise DesadvError(f"{what} 不允许含组件分隔符",
                          byte_offset=segment.start, end_byte_offset=segment.end,
                          segment_tag=segment.tag)
    return comps[0]


def _check_len(segment: Segment, expected: int) -> None:
    """段元素数必须恰好为 expected（含标签元素在内）。"""
    if len(segment.elements) != expected:
        raise DesadvError(
            f"{segment.tag} 段应有 {expected - 1} 个数据元素，"
            f"实际 {len(segment.elements) - 1} 个（多元素/缺元素均不接受）",
            byte_offset=segment.start, end_byte_offset=segment.end,
            segment_tag=segment.tag)


def _nonempty(value: str, what: str, segment: Segment) -> None:
    if value == "":
        raise DesadvError(f"{what} 不能为空",
                          byte_offset=segment.start, end_byte_offset=segment.end,
                          segment_tag=segment.tag)


def _parse_calendar_date(value: str, segment: Segment) -> str:
    if not _DATE_RE.match(value):
        raise DesadvError("DTM 日期必须是 YYYYMMDD 八位数字",
                          byte_offset=segment.start, end_byte_offset=segment.end,
                          segment_tag="DTM")
    try:
        date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError as exc:
        raise DesadvError(f"DTM 日期不是真实日历日期：{value}",
                          byte_offset=segment.start, end_byte_offset=segment.end,
                          segment_tag="DTM") from exc
    return value


def _check_unb_segment(segment: Segment, chars: ServiceChars,
                       una_span: Optional[tuple[int, int]]) -> ExchangeHeader:
    """校验 UNB 段并产出交换头（供 service 层在事务前识别重传身份）。"""
    _check_len(segment, 6)
    if _components(segment, 1) != ["UNOC", "3"]:
        raise DesadvError("UNB 语法标识必须为 UNOC:3",
                          byte_offset=segment.start,
                          end_byte_offset=segment.end, segment_tag="UNB")
    sender = _single(segment, 2, "发送方")
    receiver = _single(segment, 3, "接收方")
    _nonempty(sender, "发送方", segment)
    _nonempty(receiver, "接收方", segment)
    dt = _components(segment, 4)
    if len(dt) != 2 or not _UNB_DATE_RE.match(dt[0]) \
            or not _UNB_TIME_RE.match(dt[1]):
        raise DesadvError("UNB 日期时间必须为 YYMMDD:HHMM 且均为数字",
                          byte_offset=segment.start,
                          end_byte_offset=segment.end, segment_tag="UNB")
    yy, mm, dd = int(dt[0][0:2]), int(dt[0][2:4]), int(dt[0][4:6])
    hh, minute = int(dt[1][0:2]), int(dt[1][2:4])
    try:
        date(2000 + yy, mm, dd)
        if not (0 <= hh <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise DesadvError(
            f"UNB 日期时间不是真实日期时间：{dt[0]}:{dt[1]}",
            byte_offset=segment.start, end_byte_offset=segment.end,
            segment_tag="UNB") from exc
    control = _single(segment, 5, "交换号")
    _nonempty(control, "交换号", segment)
    return ExchangeHeader(
        sender=sender, receiver=receiver,
        date_ymd=dt[0], time_hhmm=dt[1], control=control,
        start=segment.start, end=segment.end,
        has_una=una_span is not None,
        una_start=una_span[0] if una_span else None,
        una_end=una_span[1] if una_span else None,
        decimal_char=chr(chars.decimal))


def _normalize_decimal(raw: str, chars: ServiceChars,
                       segment: Segment) -> tuple[Fraction, str]:
    """按当前交换小数符解释数量；拒绝指数/混用/无意义写法，返回精确值与点小数文本。"""
    sep = re.escape(chr(chars.decimal))
    if not re.match(rf"^\d+(?:{sep}\d+)?$", raw):
        raise DesadvError(
            "零件数量必须是正的有限十进制（禁止指数写法、符号或混用小数符）",
            byte_offset=segment.start, end_byte_offset=segment.end,
            segment_tag="QTY")
    dotted = raw.replace(chr(chars.decimal), ".")
    value = Fraction(dotted)         # 十进制字符串 -> 精确有理数，不经 float
    if value <= 0:
        raise DesadvError("零件数量必须为正数",
                          byte_offset=segment.start, end_byte_offset=segment.end,
                          segment_tag="QTY")
    return value, _decimal_from_fraction(value)


def _decimal_from_fraction(value: Fraction) -> str:
    """精确十进制展开（输入受正则限制，分母只含 2/5 因子）。"""
    den = value.denominator
    n2 = n5 = 0
    while den % 2 == 0:
        den //= 2
        n2 += 1
    while den % 5 == 0:
        den //= 5
        n5 += 1
    if den != 1:  # 理论不可达：输入为有限十进制
        raise DesadvError("数量不是有限十进制")
    places = max(n2, n5)
    if places == 0:
        return str(value.numerator)
    scaled = value.numerator * (10 ** places) // value.denominator
    digits = str(abs(scaled)).zfill(places + 1)
    sign = "-" if scaled < 0 else ""
    return f"{sign}{digits[:-places]}.{digits[-places:]}"


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
class ExchangeParser:
    """消费一个交换（UNB..UNZ）的全部段并做完整校验，边解析边落 sink。"""

    def __init__(self, sink: ParseSink, streamer, chars: ServiceChars,
                 exchange_seq: int,
                 una_span: Optional[tuple[int, int]],
                 pre_parsed_unb=None) -> None:
        self.sink = sink
        self.streamer = streamer
        self.chars = chars
        self.exchange_seq = exchange_seq
        self.una_span = una_span
        self._pre_parsed_unb = pre_parsed_unb   # (ExchangeHeader, UNB Segment)
        self._state = _S_UNB
        self._exchange_id: Optional[int] = None
        self._message_id: Optional[int] = None
        self._seg_exchange_seq = 0
        # 交换级唯一性
        self._message_nos: set[str] = set()
        self._doc_nos: set[str] = set()
        self._message_count = 0
        self._unb_control = ""
        # 消息级工作集
        self._msg_seq = 0
        self._seg_msg_seq = 0
        self._message_no = ""
        self._doc_no = self._doc_date = ""
        self._su_code = self._by_code = ""
        self._nad_seen: set[str] = set()
        self._package_ids: set[str] = set()
        self._labels: set[str] = set()
        self._line_nos: set[str] = set()
        self._packages: Dict[str, dict] = {}
        self._current: Optional[dict] = None
        self._group_state = _G_NONE
        self._last_line_no: Optional[str] = None
        self._note_seq = 0
        self._pending_lin: Optional[dict] = None

    def run(self) -> None:
        # service 层可能已预读 UNB 用于重传身份识别：先重放该段
        if self._pre_parsed_unb is not None:
            header, unb_segment = self._pre_parsed_unb
            self._seg_exchange_seq += 1
            self._handle_unb(unb_segment, header)
        while True:
            segment = self.streamer.next_segment()
            if segment is None:
                raise DesadvError("交换在 UNZ 前被截断",
                                  exchange_seq=self.exchange_seq)
            self._seg_exchange_seq += 1
            handler = getattr(self, f"_on_{segment.tag.lower()}", None)
            if handler is None:
                raise DesadvError(f"未列出的段 {segment.tag}，本子集不接受",
                                  byte_offset=segment.start,
                                  end_byte_offset=segment.end,
                                  segment_tag=segment.tag,
                                  exchange_seq=self.exchange_seq)
            handler(segment)
            if segment.tag == "UNZ":
                return

    # ------------------------------------------------------------------ #
    # 交换头尾
    # ------------------------------------------------------------------ #
    def _on_unb(self, segment: Segment) -> None:
        if self._state != _S_UNB:
            raise DesadvError("交换必须以 UNB 开始且只允许一个 UNB",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="UNB",
                              exchange_seq=self.exchange_seq)
        header = _check_unb_segment(segment, self.chars, self.una_span)
        self._handle_unb(segment, header)

    def _handle_unb(self, segment: Segment, header: ExchangeHeader) -> None:
        self._unb_control = header.control
        # 交换行由 service 层在完成重传/冲突判定后统一创建；
        # 解析器只登记该交换在 sink 中的行 id。
        self._exchange_id = self.sink.attach_exchange(header)
        self._record_segment(segment)
        self._state = _S_UNH

    def _on_unz(self, segment: Segment) -> None:
        if self._state == _S_UNB:
            raise DesadvError("出现 UNZ 但交换没有 UNB",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="UNZ",
                              exchange_seq=self.exchange_seq)
        if self._state != _S_MSG_DONE:
            raise DesadvError("UNZ 前最后一条消息没有 UNT 收尾",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="UNZ",
                              exchange_seq=self.exchange_seq)
        _check_len(segment, 3)
        count_text = _single(segment, 1, "UNZ 消息数")
        control = _single(segment, 2, "UNZ 交换号")
        if not _POSITIVE_INT_RE.match(count_text) or int(count_text) == 0:
            raise DesadvError("UNZ 消息数必须为正整数",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="UNZ")
        if int(count_text) != self._message_count:
            raise DesadvError(
                f"UNZ 消息数 {count_text} 与实际消息数 "
                f"{self._message_count} 不符",
                byte_offset=segment.start, end_byte_offset=segment.end,
                segment_tag="UNZ", exchange_seq=self.exchange_seq)
        if control != self._unb_control:
            raise DesadvError(
                f"UNZ 交换号 {control!r} 与 UNB {self._unb_control!r} 不一致",
                byte_offset=segment.start, end_byte_offset=segment.end,
                segment_tag="UNZ", exchange_seq=self.exchange_seq)
        self._record_segment(segment)

    # ------------------------------------------------------------------ #
    # 消息头
    # ------------------------------------------------------------------ #
    def _on_unh(self, segment: Segment) -> None:
        if self._state not in (_S_UNH, _S_MSG_DONE):
            raise DesadvError("UNH 位置不合法（消息必须以 UNH 开始）",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="UNH",
                              exchange_seq=self.exchange_seq)
        _check_len(segment, 3)
        message_no = _single(segment, 1, "消息号")
        _nonempty(message_no, "消息号", segment)
        if _components(segment, 2) != _MESSAGE_TYPE:
            raise DesadvError("UNH 消息类型必须为 DESADV:D:96A:UN（四个组件）",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="UNH")
        if message_no in self._message_nos:
            raise DesadvError(f"交换内消息号重复：{message_no}",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="UNH",
                              exchange_seq=self.exchange_seq)
        self._message_nos.add(message_no)
        self._message_no = message_no
        self._msg_seq += 1
        self._message_count = self._msg_seq
        self._seg_msg_seq = 1
        self._nad_seen = set()
        self._package_ids = set()
        self._labels = set()
        self._line_nos = set()
        self._packages = {}
        self._current = None
        self._group_state = _G_NONE
        self._state = _S_BGM
        # 消息行先占位（BGM 前拿不到通知单号，用 begin_message 的缓存头补齐）
        self._message_id = self.sink.begin_message(
            self._exchange_id, self._msg_seq,
            MessageHeader(message_no=message_no, doc_no="", doc_date="",
                          su_code="", by_code=""))
        self._record_segment(segment)

    def _on_bgm(self, segment: Segment) -> None:
        if self._state != _S_BGM:
            raise DesadvError("BGM 必须紧跟 UNH",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="BGM")
        _check_len(segment, 4)
        if _single(segment, 1, "BGM 消息名") != "351":
            raise DesadvError("BGM 消息名必须为 351",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="BGM")
        doc_no = _single(segment, 2, "通知单号")
        _nonempty(doc_no, "通知单号", segment)
        if _single(segment, 3, "BGM 响应码") != "9":
            raise DesadvError("BGM 第三元素必须为 9",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="BGM")
        if doc_no in self._doc_nos:
            raise DesadvError(f"交换内通知单号重复：{doc_no}",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="BGM",
                              exchange_seq=self.exchange_seq)
        self._doc_nos.add(doc_no)
        self._doc_no = doc_no
        self.sink.update_message_header(
            self._message_id, doc_no=doc_no, doc_date="",
            su_code="", by_code="", partial=True)
        self._advance(segment, _S_DTM)

    def _on_dtm(self, segment: Segment) -> None:
        if self._state != _S_DTM:
            raise DesadvError("DTM 必须紧跟 BGM",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="DTM")
        _check_len(segment, 2)
        comps = _components(segment, 1)
        if len(comps) != 3 or comps[0] != "137" or comps[2] != "102":
            raise DesadvError("DTM 必须为 137:YYYYMMDD:102",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="DTM")
        self._doc_date = _parse_calendar_date(comps[1], segment)
        self.sink.update_message_header(
            self._message_id, doc_no=self._doc_no, doc_date=self._doc_date,
            su_code="", by_code="", partial=True)
        self._advance(segment, _S_NAD)

    def _on_nad(self, segment: Segment) -> None:
        if self._state != _S_NAD:
            raise DesadvError("两条 NAD 必须位于 DTM 与包装组之间",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="NAD")
        _check_len(segment, 3)
        qualifier = _single(segment, 1, "NAD 限定符")
        if qualifier not in _NAD_QUALIFIERS:
            raise DesadvError("NAD 限定符只接受 SU 或 BY",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="NAD")
        if qualifier in self._nad_seen:
            raise DesadvError(f"NAD {qualifier} 在消息内重复",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="NAD")
        code_comps = _components(segment, 2)
        if len(code_comps) != 3 or code_comps[0] == "" \
                or code_comps[1] != "" or code_comps[2] != "92":
            raise DesadvError(
                "NAD 企业编码必须为 编码::92（第二组件留空、第三组件为 92）",
                byte_offset=segment.start, end_byte_offset=segment.end,
                segment_tag="NAD")
        self._nad_seen.add(qualifier)
        if qualifier == "SU":
            self._su_code = code_comps[0]
        else:
            self._by_code = code_comps[0]
        self._record_segment(segment)
        if len(self._nad_seen) == 2:
            self.sink.update_message_header(
                self._message_id, doc_no=self._doc_no,
                doc_date=self._doc_date, su_code=self._su_code,
                by_code=self._by_code, partial=False)
            self._state = _S_BODY

    # ------------------------------------------------------------------ #
    # 包装组
    # ------------------------------------------------------------------ #
    def _on_cps(self, segment: Segment) -> None:
        if self._state != _S_BODY:
            raise DesadvError("CPS 只能出现在两条 NAD 之后",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="CPS")
        self._close_group(segment.start)
        _check_len(segment, 3)
        package_id = _single(segment, 1, "包装号")
        _nonempty(package_id, "包装号", segment)
        parent_comps = _components(segment, 2)
        if len(parent_comps) != 1:
            raise DesadvError("CPS 父包装号不允许含组件分隔符",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="CPS")
        parent_id = parent_comps[0] or None
        if package_id in self._package_ids:
            raise DesadvError(f"消息内包装号重复：{package_id}",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="CPS")
        self._package_ids.add(package_id)
        self._current = {
            "package_id": package_id, "parent_id": parent_id,
            "pieces": None, "package_type": None, "label": None,
            "cps_start": segment.start, "cps_end": None,
        }
        self._packages[package_id] = self._current
        # 先落包装行（深度与组结束偏移在 UNT 图校验后回写），
        # 保证后续发货行的外键始终可解析。
        self.sink.add_package(self._message_id, PackageRecord(
            package_id=package_id, parent_id=parent_id, pieces=0,
            package_type="", label=None, depth=0,
            cps_start=segment.start, cps_end=segment.start))
        self._group_state = _G_AFTER_CPS
        self._last_line_no = None
        self._note_seq = 0
        self._record_segment(segment)

    def _on_pac(self, segment: Segment) -> None:
        self._require_group(segment, "PAC")
        if self._group_state != _G_AFTER_CPS:
            raise DesadvError("每个 CPS 后必须恰有一个 PAC",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="PAC")
        _check_len(segment, 4)
        pieces_text = _single(segment, 1, "PAC 件数")
        if not _POSITIVE_INT_RE.match(pieces_text) or int(pieces_text) == 0:
            raise DesadvError("PAC 件数必须为正整数",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="PAC")
        if _components(segment, 2) != [""]:
            raise DesadvError("PAC 第二数据元素必须留空",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="PAC")
        package_type = _single(segment, 3, "包装类型")
        _nonempty(package_type, "包装类型", segment)
        self._current["pieces"] = int(pieces_text)
        self._current["package_type"] = package_type
        self.sink.upsert_package_fields(
            self._message_id, self._current["package_id"],
            pieces=int(pieces_text), package_type=package_type)
        self._group_state = _G_AFTER_PAC
        self._record_segment(segment)

    def _on_gin(self, segment: Segment) -> None:
        self._require_group(segment, "GIN")
        if self._group_state != _G_AFTER_PAC:
            raise DesadvError("GIN 必须位于 PAC 之后、零件项之前，且至多一个",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="GIN")
        _check_len(segment, 3)
        if _single(segment, 1, "GIN 限定符") != "BJ":
            raise DesadvError("GIN 限定符必须为 BJ",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="GIN")
        label = _single(segment, 2, "标签号")
        _nonempty(label, "标签号", segment)
        if label in self._labels:
            raise DesadvError(f"消息内标签号重复：{label}",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="GIN")
        self._labels.add(label)
        self._current["label"] = label
        self.sink.upsert_package_fields(
            self._message_id, self._current["package_id"], label=label)
        self._group_state = _G_AFTER_GIN
        self._record_segment(segment)

    def _on_lin(self, segment: Segment) -> None:
        self._require_group(segment, "LIN")
        if self._group_state in (_G_AFTER_CPS,):
            raise DesadvError("CPS 后缺少 PAC，不能直接出现 LIN",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="LIN")
        if self._pending_lin is not None:
            raise DesadvError("上一条 LIN 缺少 QTY",
                              byte_offset=self._pending_lin["lin_start"],
                              end_byte_offset=self._pending_lin["lin_end"],
                              segment_tag="LIN")
        _check_len(segment, 4)
        line_no = _single(segment, 1, "行号")
        _nonempty(line_no, "行号", segment)
        if _components(segment, 2) != [""]:
            raise DesadvError("LIN 第二数据元素必须留空",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="LIN")
        part_comps = _components(segment, 3)
        if len(part_comps) != 2 or part_comps[0] == "" \
                or part_comps[1] != "IN":
            raise DesadvError("LIN 零件号必须为 零件号:IN",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="LIN")
        if line_no in self._line_nos:
            raise DesadvError(f"消息内行号重复：{line_no}",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="LIN")
        self._line_nos.add(line_no)
        self._pending_lin = {
            "line_no": line_no, "part_no": part_comps[0],
            "lin_start": segment.start, "lin_end": segment.end,
        }
        self._record_segment(segment)

    def _on_qty(self, segment: Segment) -> None:
        self._require_group(segment, "QTY")
        if self._pending_lin is None:
            raise DesadvError("QTY 必须紧跟其所属 LIN",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="QTY")
        _check_len(segment, 2)
        comps = _components(segment, 1)
        if len(comps) != 3 or comps[0] != "12" \
                or comps[2] not in _ALLOWED_UNITS:
            raise DesadvError("QTY 必须为 12:数量:单位，单位仅接受 PCE/KGM",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="QTY")
        quantity, quantity_text = _normalize_decimal(
            comps[1], self.chars, segment)
        pending = self._pending_lin
        self.sink.add_line(self._message_id, LineRecord(
            line_no=pending["line_no"], part_no=pending["part_no"],
            quantity=quantity, unit=comps[2], quantity_text=quantity_text,
            package_id=self._current["package_id"],
            lin_start=pending["lin_start"], lin_end=pending["lin_end"],
            qty_start=segment.start, qty_end=segment.end))
        self._pending_lin = None
        self._last_line_no = pending["line_no"]
        self._note_seq = 0
        self._group_state = _G_AFTER_LINQTY
        self._record_segment(segment)

    def _on_ftx(self, segment: Segment) -> None:
        self._require_group(segment, "FTX")
        if self._group_state != _G_AFTER_LINQTY or self._last_line_no is None:
            raise DesadvError("FTX 必须位于某条 LIN+QTY 之后",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="FTX")
        _check_len(segment, 5)
        if _single(segment, 1, "FTX 主题") != "AAI":
            raise DesadvError("FTX 主题必须为 AAI",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="FTX")
        if _components(segment, 2) != [""] or _components(segment, 3) != [""]:
            raise DesadvError("FTX 第二、第三数据元素必须留空",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="FTX")
        text = _single(segment, 4, "备注")
        _nonempty(text, "FTX 备注", segment)
        self._note_seq += 1
        self.sink.add_note(self._message_id, NoteRecord(
            line_no=self._last_line_no, seq=self._note_seq,
            text=text, start=segment.start, end=segment.end))
        self._record_segment(segment)

    # ------------------------------------------------------------------ #
    def _on_unt(self, segment: Segment) -> None:
        if self._state != _S_BODY:
            raise DesadvError("UNT 位置不合法",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="UNT",
                              exchange_seq=self.exchange_seq)
        self._close_group(segment.start)
        _check_len(segment, 3)
        count_text = _single(segment, 1, "UNT 段数")
        message_no = _single(segment, 2, "UNT 消息号")
        if not _POSITIVE_INT_RE.match(count_text) or int(count_text) == 0:
            raise DesadvError("UNT 段数必须为正整数",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag="UNT")
        if message_no != self._message_no:
            raise DesadvError(
                f"UNT 消息号 {message_no!r} 与 UNH {self._message_no!r} 不一致",
                byte_offset=segment.start, end_byte_offset=segment.end,
                segment_tag="UNT", exchange_seq=self.exchange_seq)
        self._record_segment(segment)   # UNT 本身计入段数
        if int(count_text) != self._seg_msg_seq:
            raise DesadvError(
                f"UNT 段数 {count_text} 与实际段数 {self._seg_msg_seq} 不符"
                "（UNH 与 UNT 均计入）",
                byte_offset=segment.start, end_byte_offset=segment.end,
                segment_tag="UNT", exchange_seq=self.exchange_seq)
        self._check_package_graph()
        self._state = _S_MSG_DONE

    # ------------------------------------------------------------------ #
    # 组/层级
    # ------------------------------------------------------------------ #
    def _require_group(self, segment: Segment, tag: str) -> None:
        if self._state != _S_BODY or self._current is None:
            raise DesadvError(f"{tag} 必须位于某个 CPS 包装组内",
                              byte_offset=segment.start,
                              end_byte_offset=segment.end, segment_tag=tag,
                              exchange_seq=self.exchange_seq)

    def _close_group(self, next_start: int) -> None:
        if self._current is None:
            return
        if self._pending_lin is not None:
            raise DesadvError("LIN 后缺少 QTY 就结束了包装组",
                              byte_offset=self._pending_lin["lin_start"],
                              end_byte_offset=self._pending_lin["lin_end"],
                              segment_tag="LIN",
                              exchange_seq=self.exchange_seq)
        if self._group_state == _G_AFTER_CPS:
            raise DesadvError(
                f"CPS {self._current['package_id']} 后缺少必需的 PAC",
                byte_offset=self._current["cps_start"],
                end_byte_offset=self._current["cps_start"] + 3,
                segment_tag="CPS", exchange_seq=self.exchange_seq)
        cur = self._current
        cur["cps_end"] = next_start
        self._current = None
        self._group_state = _G_NONE

    def _check_package_graph(self) -> None:
        """父引用、自指、成环检查 + 深度计算（允许父项后到），随后逐包落库。"""
        packages = self._packages
        for pkg_id, pkg in packages.items():
            parent = pkg["parent_id"]
            if parent is not None and parent not in packages:
                raise DesadvError(
                    f"包装 {pkg_id} 引用了不存在的父包装 {parent}",
                    byte_offset=pkg["cps_start"], end_byte_offset=pkg["cps_end"],
                    segment_tag="CPS", exchange_seq=self.exchange_seq)
            if parent == pkg_id:
                raise DesadvError(f"包装 {pkg_id} 自指为父包装",
                                  byte_offset=pkg["cps_start"],
                                  end_byte_offset=pkg["cps_end"],
                                  segment_tag="CPS",
                                  exchange_seq=self.exchange_seq)
        color: Dict[str, int] = {}
        depth: Dict[str, int] = {}

        def visit(node: str, stack: List[str]) -> int:
            if color.get(node) == 1:
                cycle = " -> ".join(stack[stack.index(node):] + [node])
                raise DesadvError(f"包装层级存在环：{cycle}",
                                  byte_offset=packages[node]["cps_start"],
                                  end_byte_offset=packages[node]["cps_end"],
                                  segment_tag="CPS",
                                  exchange_seq=self.exchange_seq)
            if color.get(node) == 2:
                return depth[node]
            color[node] = 1
            stack.append(node)
            parent = packages[node]["parent_id"]
            depth[node] = 0 if parent is None else visit(parent, stack) + 1
            stack.pop()
            color[node] = 2
            return depth[node]

        for pkg_id in packages:
            visit(pkg_id, [])
        for pkg_id, pkg in packages.items():
            self.sink.update_package_depth(
                self._message_id, pkg_id, depth[pkg_id], pkg["cps_end"])

    # ------------------------------------------------------------------ #
    def _advance(self, segment: Segment, new_state: int) -> None:
        self._record_segment(segment)
        self._state = new_state

    def _record_segment(self, segment: Segment) -> None:
        """登记一个段。UNH 在 _on_unh 内已把消息内序号置 1，不重复递增。"""
        if segment.tag != "UNH":
            self._seg_msg_seq += 1
        self.sink.add_segment(
            self._exchange_id, self._message_id, segment,
            self._seg_msg_seq, self._seg_exchange_seq)
