"""tokenizer 单元测试：UNA 校验、释放符、跨块边界与字节偏移。"""
from __future__ import annotations

import io

import pytest

from mes_pipeline.desadv.errors import DesadvError
from mes_pipeline.desadv.tokenizer import (
    SegmentStreamer,
    default_service_chars,
)


def parse_all(data: bytes, block_size: int):
    s = SegmentStreamer(io.BytesIO(data).read, block_size=block_size)
    chars, una_span = s.begin_exchange()
    segments = []
    while True:
        seg = s.next_segment()
        if seg is None:
            break
        segments.append(seg)
    return chars, una_span, segments


def test_default_service_chars():
    c = default_service_chars()
    assert (chr(c.component), chr(c.element), chr(c.decimal),
            chr(c.release), chr(c.reserve), chr(c.terminator)) == \
        (":", "+", ".", "?", " ", "'")


def test_basic_split_preserves_empty_elements():
    data = b"UNA:+.? '\nUNB+UNOC:3+S++R+260101:0900+X'\n"
    _, _, segs = parse_all(data, block_size=4)
    unb = segs[0]
    assert unb.tag == "UNB"
    # 连续分隔产生的空元素必须保留
    assert unb.elements[3] == [""]
    assert unb.elements[1] == ["UNOC", "3"]
    # 字节偏移左闭右开、含段结束符
    assert data[unb.start:unb.end] == unb.raw
    assert unb.raw.endswith(b"'")


@pytest.mark.parametrize("block_size", [1, 2, 3, 5, 7, 64, 10 ** 6])
def test_block_size_equivalence(block_size):
    data = (b"UNA:+.? '\nUNH+1+DESADV:D:96A:UN'\n"
            b"FTX+AAI+++'\xe4\xb8\xad?? ?+ ?''\n")
    chars, _, small = parse_all(data, block_size)
    _, _, big = parse_all(data, 10 ** 6)
    assert [s.elements for s in small] == [s.elements for s in big]
    assert [(s.start, s.end) for s in small] == [(s.start, s.end) for s in big]


def test_release_makes_service_char_data():
    # ?? -> ?   ?+ -> +   ?' -> '
    data = b"UNA:+.? '\nFTX+AAI+++a??b?+c?'d'\n"
    _, _, segs = parse_all(data, block_size=2)
    assert segs[0].elements[4][0] == "a?b+c'd"


def test_meaningless_release_rejected():
    data = b"UNA:+.? '\nFTX+AAI+++a?b'\n"   # ? 后是普通字符 b
    with pytest.raises(DesadvError, match="无意义释放"):
        parse_all(data, block_size=3)


def test_dangling_release_at_eof_rejected():
    data = b"UNA:+.? '\nFTX+AAI+++ab?"       # 释放符后文件结束
    with pytest.raises(DesadvError, match="悬空释放"):
        parse_all(data, block_size=5)


def test_missing_terminator_rejected():
    data = b"UNA:+.? '\nUNB+UNOC:3"          # 无段结束符即 EOF
    with pytest.raises(DesadvError, match="段缺少段结束符"):
        parse_all(data, block_size=4)


def test_chinese_char_split_across_blocks():
    text = "FTX+AAI+++中文备注'".encode("utf-8")
    data = b"UNA:+.? '\n" + text + b"\n"
    for block_size in range(1, 8):
        _, _, segs = parse_all(data, block_size)
        assert segs[0].elements[4][0] == "中文备注"


def test_release_sequence_split_across_blocks():
    # ?+ 被块边界切开
    data = b"UNA:+.? '\nFTX+AAI+++ab?+cd'\n"
    for block_size in range(1, 12):
        _, _, segs = parse_all(data, block_size)
        assert segs[0].elements[4][0] == "ab+cd"


def test_una_split_across_blocks():
    data = b"UNA:+.? '\nUNB+A'\n"
    for block_size in range(1, 10):
        chars, span, _ = parse_all(data, block_size)
        assert span == (0, 9)
        assert chr(chars.terminator) == "'"


def test_custom_una_and_decimal_comma():
    data = b"UNA|^,\\ ~\nUNB^UNOC|3^S^R^260101^0900^X~\n"
    chars, span, segs = parse_all(data, block_size=3)
    assert span == (0, 9)
    assert (chr(chars.component), chr(chars.element), chr(chars.decimal),
            chr(chars.release), chr(chars.terminator)) == \
        ("|", "^", ",", "\\", "~")
    assert segs[0].elements[1] == ["UNOC", "3"]


@pytest.mark.parametrize("bad_una", [
    b"UNA:+.?x'",     # 保留位不是空格（x）
    b"UNA:+.x? '",    # 小数符不是点/逗号（x）
    b"UNA:+.?? '",    # 服务字符重复（释放符=段结束符=?）
    b"UNA:+.?. '",    # 服务字符与小数符相同（段结束符=.）
    b"UNA:+.?1 '",    # 服务字符不是 ASCII 标点（1 是数字）
])
def test_bad_una_rejected(bad_una):
    with pytest.raises(DesadvError):
        parse_all(bad_una + b"\n", block_size=4)


def test_invalid_utf8_rejected():
    data = b"UNA:+.? '\nFTX+AAI+++\xff\xfe'\n"
    with pytest.raises(DesadvError, match="UTF-8"):
        parse_all(data, block_size=3)


def test_segment_length_limit():
    data = b"UNA:+.? '\n" + b"X" * 10 + b"'\n"
    s = SegmentStreamer(io.BytesIO(data).read, block_size=4,
                        max_segment_bytes=8)
    s.begin_exchange()
    with pytest.raises(DesadvError, match="单段长度超过上限"):
        s.next_segment()


def test_multiple_exchanges_each_own_una():
    e1 = b"UNA:+.? '\nUNB+A'\nUNZ+1+A'\n"
    e2 = b"UNA|^,\\ ~\nUNB^B~\nUNZ^1^B~\n"
    s = SegmentStreamer(io.BytesIO(e1 + e2).read, block_size=5)
    c1, _ = s.begin_exchange()
    assert chr(c1.terminator) == "'"
    assert s.next_segment().tag == "UNB"
    assert s.next_segment().tag == "UNZ"
    c2, _ = s.begin_exchange()
    assert chr(c2.terminator) == "~"
    assert s.next_segment().tag == "UNB"
    assert s.next_segment().tag == "UNZ"
    assert s.begin_exchange() is None
