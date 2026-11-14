"""parser 状态机测试：结构校验、层级图、精确数量与错误定位。"""
from __future__ import annotations

import io
from fractions import Fraction

import pytest

from mes_pipeline.desadv.errors import DesadvError
from mes_pipeline.desadv.parser import ExchangeParser
from mes_pipeline.desadv.tokenizer import SegmentStreamer
from tests._desadv_builder import EdiTextBuilder, minimal_exchange

CHARS = {"comp": ":", "elem": "+", "decimal": ".", "release": "?", "term": "'"}


class RecordingSink:
    """记录解析产物，便于断言；不落数据库。"""

    def __init__(self):
        self.messages = []
        self.segments = []
        self.packages = []
        self.lines = []
        self.notes = []
        self._depth = {}

    def attach_exchange(self, header):
        return -1

    def begin_message(self, exchange_id, seq, header):
        self.messages.append(header)
        return len(self.messages)

    def update_message_header(self, message_id, *, doc_no, doc_date,
                              su_code, by_code, partial):
        # 模拟 store 的补写：用非空字段更新已登记的消息头
        header = self.messages[message_id - 1]
        if doc_no:
            object.__setattr__(header, "doc_no", doc_no)
        if doc_date:
            object.__setattr__(header, "doc_date", doc_date)
        if su_code:
            object.__setattr__(header, "su_code", su_code)
        if by_code:
            object.__setattr__(header, "by_code", by_code)

    def add_segment(self, *args):
        self.segments.append(args)

    def add_package(self, message_id, record):
        self.packages.append(record)

    def upsert_package_fields(self, *a, **k):
        pass

    def update_package_depth(self, message_id, package_code, depth, cps_end):
        self._depth[package_code] = depth

    def add_line(self, message_id, record):
        self.lines.append(record)

    def add_note(self, message_id, record):
        self.notes.append(record)


def run_parser(data: bytes, block_size: int = 16) -> RecordingSink:
    sink = RecordingSink()
    s = SegmentStreamer(io.BytesIO(data).read, block_size=block_size)
    chars, una_span = s.begin_exchange()
    ExchangeParser(sink, s, chars, 1, una_span).run()
    return sink


def test_minimal_valid_exchange():
    sink = run_parser(minimal_exchange())
    assert len(sink.messages) == 1
    assert sink.messages[0].doc_no == "DN1"
    assert sink.lines[0].quantity == Fraction(2)
    assert sink.lines[0].unit == "PCE"
    assert sink.notes[0].text == "备注 中文"


def test_unt_segment_count_includes_both_ends():
    # 正确的 UNT=11；改成 12 必须被拒
    good = minimal_exchange()
    assert run_parser(good)
    bad = good.replace(b"UNT+11+M1", b"UNT+12+M1")
    with pytest.raises(DesadvError, match="UNT 段数"):
        run_parser(bad)


def test_unz_count_and_control_checked():
    data = minimal_exchange()
    with pytest.raises(DesadvError, match="UNZ"):
        run_parser(data.replace(b"UNZ+1+EX1", b"UNZ+2+EX1"))
    with pytest.raises(DesadvError, match="UNZ"):
        run_parser(data.replace(b"UNZ+1+EX1", b"UNZ+1+OTHER"))


def test_unknown_segment_rejected():
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    b.seg("XXX", "1")
    b.seg("UNZ", "1", "X")
    with pytest.raises(DesadvError, match="未列出的段"):
        run_parser(b.bytes())


def test_extra_and_missing_elements_rejected():
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X", "EXTRA")
    b.seg("UNZ", "1", "X")
    with pytest.raises(DesadvError, match="数据元素"):
        run_parser(b.bytes())


def test_nad_order_free_but_no_duplicate():
    # BY 在 SU 前合法
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    b.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "D", "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "BY", ["B", "", "92"])
    b.seg("NAD", "SU", ["S", "", "92"])
    b.seg("UNT", "6", "M")
    b.seg("UNZ", "1", "X")
    assert run_parser(b.bytes())
    # 消息段：UNH,BGM,DTM,NAD,NAD,UNT = 6
    bad = EdiTextBuilder()
    bad.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "Y")
    bad.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
    bad.seg("BGM", "351", "D", "9")
    bad.seg("DTM", ["137", "20260101", "102"])
    bad.seg("NAD", "SU", ["S", "", "92"])
    bad.seg("NAD", "SU", ["S2", "", "92"])
    with pytest.raises(DesadvError, match="NAD SU 在消息内重复"):
        run_parser(bad.bytes())


def test_invalid_calendar_date_rejected():
    data = minimal_exchange().replace(b"20260101", b"20260230")
    with pytest.raises(DesadvError, match="真实日历日期"):
        run_parser(data)


def test_parent_after_child_resolves_and_depth():
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    b.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "D", "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "SU", ["S", "", "92"])
    b.seg("NAD", "BY", ["B", "", "92"])
    b.seg("CPS", "CHILD", "ROOT")     # 父后到
    b.seg("PAC", "1", "", "CT")
    b.seg("CPS", "ROOT", "")
    b.seg("PAC", "1", "", "PAL")
    b.seg("UNT", "10", "M")
    b.seg("UNZ", "1", "X")
    sink = run_parser(b.bytes())
    assert sink._depth == {"CHILD": 1, "ROOT": 0}


def test_orphan_self_cycle_and_duplicate_package():
    def base():
        b = EdiTextBuilder()
        b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
        b.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
        b.seg("BGM", "351", "D", "9")
        b.seg("DTM", ["137", "20260101", "102"])
        b.seg("NAD", "SU", ["S", "", "92"])
        b.seg("NAD", "BY", ["B", "", "92"])
        return b

    b = base()
    b.seg("CPS", "A", "GHOST")
    b.seg("PAC", "1", "", "CT")
    b.seg("UNT", "8", "M")
    b.seg("UNZ", "1", "X")
    with pytest.raises(DesadvError, match="不存在的父包装"):
        run_parser(b.bytes())

    b = base()
    b.seg("CPS", "A", "A")
    b.seg("PAC", "1", "", "CT")
    b.seg("UNT", "8", "M")
    b.seg("UNZ", "1", "X")
    with pytest.raises(DesadvError, match="自指"):
        run_parser(b.bytes())

    b = base()
    b.seg("CPS", "A", "B")
    b.seg("PAC", "1", "", "CT")
    b.seg("CPS", "B", "A")
    b.seg("PAC", "1", "", "CT")
    b.seg("UNT", "10", "M")
    b.seg("UNZ", "1", "X")
    with pytest.raises(DesadvError, match="存在环"):
        run_parser(b.bytes())

    b = base()
    b.seg("CPS", "A", "")
    b.seg("PAC", "1", "", "CT")
    b.seg("CPS", "A", "")
    b.seg("PAC", "1", "", "CT")
    with pytest.raises(DesadvError, match="包装号重复"):
        run_parser(b.bytes())


def test_pac_required_and_single_gin():
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    b.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "D", "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "SU", ["S", "", "92"])
    b.seg("NAD", "BY", ["B", "", "92"])
    b.seg("CPS", "A", "")
    b.seg("LIN", "1", "", ["P", "IN"])     # 缺 PAC
    with pytest.raises(DesadvError, match="缺少 PAC"):
        run_parser(b.bytes())


def test_pieces_must_be_positive_integer():
    data = minimal_exchange().replace(b"PAC+1++CT", b"PAC+0++CT")
    with pytest.raises(DesadvError, match="PAC 件数"):
        run_parser(data)


def test_quantity_precision_no_float_and_exponent_rejected():
    data = minimal_exchange().replace(b"QTY+12:2:PCE", b"QTY+12:0.25:PCE")
    sink = run_parser(data)
    assert sink.lines[0].quantity == Fraction(1, 4)
    assert sink.lines[0].quantity_text == "0.25"
    for bad in (b"QTY+12:1e3:PCE", b"QTY+12:-1:PCE", b"QTY+12:1.2.3:PCE"):
        with pytest.raises(DesadvError):
            run_parser(minimal_exchange().replace(b"QTY+12:2:PCE", bad))


def test_decimal_comma_interpreted_per_exchange():
    chars = {"comp": "|", "elem": "^", "decimal": ",", "release": "\\",
             "term": "~"}
    b = EdiTextBuilder(chars)
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    b.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "D", "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "SU", ["S", "", "92"])
    b.seg("NAD", "BY", ["B", "", "92"])
    b.seg("CPS", "A", "")
    b.seg("PAC", "1", "", "CT")
    b.seg("LIN", "1", "", ["P", "IN"])
    b.seg("QTY", ["12", "1,5", "KGM"])
    b.seg("UNT", "10", "M")
    b.seg("UNZ", "1", "X")
    sink = run_parser(b.bytes(), block_size=5)
    assert sink.lines[0].quantity == Fraction(3, 2)
    assert sink.lines[0].quantity_text == "1.5"


def test_mixed_decimal_separator_rejected():
    # 逗号小数交换里写点号数量
    data = minimal_exchange()  # 点小数基线
    with pytest.raises(DesadvError):
        run_parser(data.replace(b"QTY+12:2:PCE", b"QTY+12:2,5:PCE"))


def test_unknown_unit_rejected():
    data = minimal_exchange().replace(b":PCE", b":XYZ")
    with pytest.raises(DesadvError, match="单位仅接受"):
        run_parser(data)


def test_duplicate_line_and_label_rejected():
    data = minimal_exchange()
    with pytest.raises(DesadvError, match="行号重复"):
        run_parser(data.replace(b"LIN+1++", b"LIN+1++").replace(
            b"FTX+AAI", b"LIN+1++PART9:IN'\nQTY+12:1:PCE'\nFTX+AAI"))
    # 标签号重复
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    b.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "D", "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "SU", ["S", "", "92"])
    b.seg("NAD", "BY", ["B", "", "92"])
    b.seg("CPS", "A", "")
    b.seg("PAC", "1", "", "CT")
    b.seg("GIN", "BJ", "SAME")
    b.seg("CPS", "B", "")
    b.seg("PAC", "1", "", "CT")
    b.seg("GIN", "BJ", "SAME")
    b.seg("UNT", "11", "M")
    b.seg("UNZ", "1", "X")
    with pytest.raises(DesadvError, match="标签号重复"):
        run_parser(b.bytes())


def test_ftx_requires_completed_lin_qty():
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    b.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "D", "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "SU", ["S", "", "92"])
    b.seg("NAD", "BY", ["B", "", "92"])
    b.seg("CPS", "A", "")
    b.seg("PAC", "1", "", "CT")
    b.seg("FTX", "AAI", "", "", "孤儿备注")
    with pytest.raises(DesadvError, match="FTX 必须位于"):
        run_parser(b.bytes())


def test_leading_zeros_and_case_preserved_in_codes():
    data = minimal_exchange().replace(b"CPS+P1+", b"CPS+007a+")
    sink = run_parser(data)
    assert sink.packages and sink.packages[0].package_id == "007a"
