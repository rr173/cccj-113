"""service/store/queries 集成测试：事务、重传、冲突、查询与字节切片。"""
from __future__ import annotations

import sqlite3

import pytest

from mes_pipeline.desadv.errors import DesadvConflict, DesadvError
from mes_pipeline.desadv.queries import (
    get_line,
    list_exchanges,
    subtree_summary,
    verify_slice,
)
from mes_pipeline.desadv.service import import_file
from tests._desadv_builder import EdiTextBuilder, minimal_exchange


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def two_box_exchange(control="EX1"):
    """根托盘 P1 后到；同零件 PART-A 跨两个箱；含中文转义备注。"""
    b = EdiTextBuilder()
    b.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0900"], control)
    b.seg("UNH", "M1", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "DN1", "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "SU", ["S", "", "92"])
    b.seg("NAD", "BY", ["B", "", "92"])
    b.seg("CPS", "B1", "P1")
    b.seg("PAC", "2", "", "CT")
    b.seg("LIN", "1", "", ["PART-A", "IN"])
    b.seg("QTY", ["12", "3", "PCE"])
    b.seg("FTX", "AAI", "", "", "中文?备注+尾")
    b.seg("CPS", "B2", "P1")
    b.seg("PAC", "1", "", "CT")
    b.seg("LIN", "2", "", ["PART-A", "IN"])
    b.seg("QTY", ["12", "1.5", "KGM"])
    b.seg("CPS", "P1", "")
    b.seg("PAC", "1", "", "PAL")
    b.seg("UNT", "17", "M1")
    b.seg("UNZ", "1", control)
    return b.bytes()


def test_import_and_relation_tables(tmp_path):
    f = write(tmp_path, "a.edi", two_box_exchange())
    db = str(tmp_path / "store.db")
    result = import_file(db, f, block_size=7)
    assert result.total_lines == 2
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM desadv_exchange").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM desadv_package").fetchone()[0] == 3
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("block_size", [1, 2, 7, 4096])
def test_block_size_invariance(tmp_path, block_size):
    data = two_box_exchange()
    ref_db = str(tmp_path / "ref.db")
    import_file(ref_db, write(tmp_path, "ref.edi", data), block_size=4096)

    db = str(tmp_path / f"b{block_size}.db")
    import_file(db, write(tmp_path, f"b{block_size}.edi", data),
                block_size=block_size)
    ref = sqlite3.connect(ref_db)
    cur = sqlite3.connect(db)
    for table in ("desadv_exchange", "desadv_message", "desadv_segment",
                  "desadv_package", "desadv_line", "desadv_ftx"):
        # 去掉自增主键列与来源文件路径后逐行比对（两库文件名不同属预期）
        skip = {"exchange_id", "message_id", "segment_id", "package_id",
                "line_id", "ftx_id", "source_file"}
        cols = [r[1] for r in ref.execute(
            f"PRAGMA table_info({table})").fetchall() if r[1] not in skip]
        select = ",".join(cols)
        assert cur.execute(
            f"SELECT {select} FROM {table} ORDER BY 1").fetchall() == \
            ref.execute(
                f"SELECT {select} FROM {table} ORDER BY 1").fetchall(), table


def test_byte_offsets_slice_back_to_raw(tmp_path):
    data = two_box_exchange()
    f = write(tmp_path, "a.edi", data)
    db = str(tmp_path / "store.db")
    import_file(db, f, block_size=3)
    conn = sqlite3.connect(db)
    for start, end, raw in conn.execute(
            "SELECT start_byte, end_byte, raw_segment FROM desadv_segment"):
        assert verify_slice(db, f, start, end) == raw
        assert data[start:end] == raw     # 偏移是字节偏移而非字符偏移


def test_line_path_and_decoded_note(tmp_path):
    f = write(tmp_path, "a.edi", two_box_exchange())
    db = str(tmp_path / "store.db")
    import_file(db, f, block_size=5)
    line = get_line(db, "M1", "1")
    assert [p["package_code"] for p in line["package_path"]] == ["P1", "B1"]
    assert line["notes"][0]["note_text"] == "中文?备注+尾"
    assert line["quantity_text"] == "3"


def test_subtree_separates_units_and_no_piece_multiplication(tmp_path):
    f = write(tmp_path, "a.edi", two_box_exchange())
    db = str(tmp_path / "store.db")
    import_file(db, f, block_size=11)
    summary = subtree_summary(db, "M1", "P1")
    totals = {(t["part_no"], t["unit"]): t["quantity_fraction"]
              for t in summary["totals"]}
    # 同零件不同单位分别汇总、不相加；不乘 PAC 件数
    assert totals[("PART-A", "PCE")] == "3/1"
    assert totals[("PART-A", "KGM")] == "3/2"
    # 单箱子树只含自身行
    b1 = subtree_summary(db, "M1", "B1")
    assert b1["subtree_line_count"] == 1


def test_duplicate_retransmit_adds_no_rows(tmp_path):
    data = minimal_exchange()
    f1 = write(tmp_path, "first.edi", data)
    f2 = write(tmp_path, "second.edi", data)
    db = str(tmp_path / "store.db")
    r1 = import_file(db, f1, block_size=7)
    r2 = import_file(db, f2, block_size=4096)
    assert r1.exchanges[0].outcome == "inserted"
    assert r2.exchanges[0].outcome == "duplicate"
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT count(*) FROM desadv_exchange").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM desadv_line").fetchone()[0] == 1


def test_same_identity_different_bytes_conflicts_and_keeps_old(tmp_path):
    good = minimal_exchange()
    bad = good.replace(b"DN1", b"DN9")       # 同 UNB 身份、不同载荷
    f1 = write(tmp_path, "good.edi", good)
    f2 = write(tmp_path, "bad.edi", bad)
    db = str(tmp_path / "store.db")
    import_file(db, f1, block_size=7)
    with pytest.raises(DesadvConflict):
        import_file(db, f2, block_size=1)
    conn = sqlite3.connect(db)
    # 旧资料不动、无半条新资料
    assert conn.execute(
        "SELECT doc_no FROM desadv_message").fetchone()[0] == "DN1"
    assert conn.execute("SELECT count(*) FROM desadv_exchange").fetchone()[0] == 1


def test_bad_exchange_rolls_back_and_earlier_survives(tmp_path):
    good = minimal_exchange(control="EX-GOOD")
    bad = EdiTextBuilder()
    bad.seg("UNB", ["UNOC", "3"], "S", "R", ["260101", "0901"], "EX-BAD")
    bad.seg("UNH", "M", ["DESADV", "D", "96A", "UN"])
    bad.seg("BGM", "351", "D", "9")
    bad.seg("DTM", ["137", "20260101", "102"])
    bad.seg("NAD", "SU", ["S", "", "92"])
    bad.seg("NAD", "BY", ["B", "", "92"])
    bad.seg("CPS", "A", "GHOST")            # 孤儿父包装
    bad.seg("PAC", "1", "", "CT")
    bad.seg("UNT", "8", "M")
    bad.seg("UNZ", "1", "EX-BAD")
    f = write(tmp_path, "mix.edi", good + bad.bytes())
    db = str(tmp_path / "store.db")
    with pytest.raises(DesadvError, match="不存在的父包装"):
        import_file(db, f, block_size=3)
    conn = sqlite3.connect(db)
    surviving = [r[0] for r in conn.execute(
        "SELECT exchange_control FROM desadv_exchange")]
    assert surviving == ["EX-GOOD"]
    # 再次重传整个文件：好交换识别为重复，坏交换仍拒绝，已提交资料不翻倍
    with pytest.raises(DesadvError):
        import_file(db, f, block_size=3)
    assert [r[0] for r in sqlite3.connect(db).execute(
        "SELECT exchange_control FROM desadv_exchange")] == ["EX-GOOD"]


def test_truncated_exchange_leaves_nothing(tmp_path):
    good = minimal_exchange(control="EX-GOOD")
    f = write(tmp_path, "trunc.edi", good + b"UNA:+.? '\nUNB+UNOC:3+S+R+")
    db = str(tmp_path / "store.db")
    with pytest.raises(DesadvError):
        import_file(db, f, block_size=4)
    conn = sqlite3.connect(db)
    assert [r[0] for r in conn.execute(
        "SELECT exchange_control FROM desadv_exchange")] == ["EX-GOOD"]


def test_list_exchanges(tmp_path):
    f = write(tmp_path, "a.edi", minimal_exchange())
    db = str(tmp_path / "store.db")
    import_file(db, f, block_size=7)
    exchanges = list_exchanges(db)
    assert len(exchanges) == 1
    assert exchanges[0]["message_count"] == 1
