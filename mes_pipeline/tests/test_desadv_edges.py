"""跨消息/跨交换唯一性与默认 UNA、空元素保留等边界测试。"""
from __future__ import annotations

import pytest

from mes_pipeline.desadv.errors import DesadvError
from mes_pipeline.desadv.parser import ExchangeParser
from mes_pipeline.desadv.tokenizer import SegmentStreamer
from tests._desadv_builder import EdiTextBuilder, minimal_exchange
from tests.test_desadv_parser import RecordingSink, run_parser


def message_block(b, msg_no, doc_no):
    b.seg("UNH", msg_no, ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", doc_no, "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "SU", ["S", "", "92"])
    b.seg("NAD", "BY", ["B", "", "92"])
    b.seg("CPS", "P", "")
    b.seg("PAC", "1", "", "CT")
    b.seg("LIN", "1", "", ["P", "IN"])
    b.seg("QTY", ["12", "1", "PCE"])
    b.seg("UNT", "10", msg_no)


def test_duplicate_message_no_within_exchange_rejected():
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    message_block(b, "DUP", "DOC1")
    message_block(b, "DUP", "DOC2")       # 消息号重复
    b.seg("UNZ", "2", "X")
    with pytest.raises(DesadvError, match="交换内消息号重复"):
        run_parser(b.bytes())


def test_duplicate_doc_no_within_exchange_rejected():
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    message_block(b, "M1", "SAME")
    message_block(b, "M2", "SAME")       # 通知单号重复
    b.seg("UNZ", "2", "X")
    with pytest.raises(DesadvError, match="交换内通知单号重复"):
        run_parser(b.bytes())


def test_two_distinct_messages_accepted():
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    message_block(b, "M1", "DOC1")
    message_block(b, "M2", "DOC2")
    b.seg("UNZ", "2", "X")
    sink = run_parser(b.bytes())
    assert [m.message_no for m in sink.messages] == ["M1", "M2"]


def test_nad_missing_qualifier_rejected():
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    b.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "D", "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "SU", ["S", "", "92"])
    b.seg("NAD", "XX", ["B", "", "92"])   # 非法限定符
    with pytest.raises(DesadvError, match="NAD 限定符"):
        run_parser(b.bytes())


def test_nad_code_requires_double_colon_92():
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], "X")
    b.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "D", "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "SU", ["S", "X", "92"])   # 第二组件必须为空
    b.seg("NAD", "BY", ["B", "", "92"])
    with pytest.raises(DesadvError, match="企业编码"):
        run_parser(b.bytes())


def test_no_una_uses_default_service_chars():
    # 无 UNA，直接 UNB 开头，使用默认 :+.? '
    data = (b"UNB+UNOC:3+S+R+260101:0900+X'\n"
            b"UNH+M+DESADV:D:96A:UN'\nBGM+351+D+9'\n"
            b"DTM+137:20260101:102'\nNAD+SU+S::92'\nNAD+BY+B::92'\n"
            b"CPS+P+'\nPAC+1++CT'\nLIN+1++P:IN'\nQTY+12:1:PCE'\n"
            b"UNT+10+M'\nUNZ+1+X'\n")
    import io
    sink = RecordingSink()
    s = SegmentStreamer(io.BytesIO(data).read, block_size=5)
    chars, una_span = s.begin_exchange()
    assert una_span is None
    assert chr(chars.terminator) == "'"
    ExchangeParser(sink, s, chars, 1, una_span).run()
    assert sink.lines[0].unit == "PCE"


def test_trailing_and_consecutive_empty_elements_kept():
    # LIN 的空第二元素、组件内空值都必须保留为显式空串
    data = minimal_exchange()
    sink = run_parser(data, block_size=2)
    # NAD 编码::92 解出三个组件，中间为空串
    import io
    s = SegmentStreamer(io.BytesIO(data).read, block_size=2)
    s.begin_exchange()
    nad = None
    while True:
        seg = s.next_segment()
        if seg is None:
            break
        if seg.tag == "NAD":
            nad = seg
            break
    assert nad.elements[2] == ["SUP1", "", "92"]


def test_crlf_between_segments_accepted():
    data = minimal_exchange().replace(b"\n", b"\r\n")
    sink = run_parser(data, block_size=3)
    assert len(sink.messages) == 1


def test_whitespace_inside_field_preserved():
    # 字段内空白按原样保留（不做 strip）
    data = minimal_exchange().replace(
        "备注 中文".encode(), "  前后空格  ".encode())
    sink = run_parser(data)
    assert sink.notes[0].text == "  前后空格  "
