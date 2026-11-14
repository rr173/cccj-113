"""关系表查询：交换/通知清单、发货行完整路径、包装子树汇总。

数量口径：

- 存储层保留每个发货行数量的精确分子/分母（由有限十进制直接得到的
  ``Fraction``），查询汇总在 Python 中做有理数加法，**不使用 float**；
- 子树汇总按（零件号, 单位）累加该包装及全部后代的发货行数量，
  PCE 与 KGM 分别汇总、互不相加、不做隐式换算，也**不乘包装件数**、
  不重复计算祖先；
- 发货行只属于其前面的那个 CPS，相同零件号跨箱不会被合并。
"""
from __future__ import annotations

import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional

from .store import open_store


def list_exchanges(db_path: str | Path) -> List[dict]:
    conn = open_store(db_path)
    try:
        rows = conn.execute(
            "SELECT e.exchange_id, e.source_file, e.sender, e.receiver, "
            "e.exchange_control, e.unb_date, e.unb_time, e.decimal_char, "
            "e.exchange_start, e.exchange_end, e.payload_hash, e.seq_in_file, "
            "COUNT(m.message_id) AS message_count "
            "FROM desadv_exchange e LEFT JOIN desadv_message m "
            "ON m.exchange_id=e.exchange_id "
            "GROUP BY e.exchange_id ORDER BY e.exchange_id").fetchall()
        return [dict(zip(_exchange_columns(), r)) for r in rows]
    finally:
        conn.close()


def _exchange_columns() -> List[str]:
    return ["exchange_id", "source_file", "sender", "receiver",
            "exchange_control", "unb_date", "unb_time", "decimal_char",
            "exchange_start", "exchange_end", "payload_hash", "seq_in_file",
            "message_count"]


def list_messages(db_path: str | Path, exchange_id: Optional[int] = None) -> List[dict]:
    conn = open_store(db_path)
    try:
        sql = ("SELECT message_id, exchange_id, seq_in_exchange, message_no, "
               "doc_no, doc_date, su_code, by_code FROM desadv_message")
        params: tuple = ()
        if exchange_id is not None:
            sql += " WHERE exchange_id=?"
            params = (exchange_id,)
        sql += " ORDER BY exchange_id, seq_in_exchange"
        return [dict(zip(["message_id", "exchange_id", "seq_in_exchange",
                          "message_no", "doc_no", "doc_date", "su_code",
                          "by_code"], r)) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def get_line(db_path: str | Path, message_no: str, line_no: str,
             source_file: Optional[str] = None) -> dict:
    """查询一条发货行：根→包装完整路径、数量、备注及全部来源定位。"""
    conn = open_store(db_path)
    try:
        msg_sql = ("SELECT m.message_id, m.exchange_id, m.message_no, m.doc_no, "
                   "m.doc_date, m.su_code, m.by_code, e.source_file, e.sender, "
                   "e.receiver, e.exchange_control "
                   "FROM desadv_message m JOIN desadv_exchange e "
                   "ON m.exchange_id=e.exchange_id WHERE m.message_no=?")
        params: list = [message_no]
        if source_file is not None:
            msg_sql += " AND e.source_file=?"
            params.append(source_file)
        msg = conn.execute(msg_sql, params).fetchone()
        if msg is None:
            raise KeyError(f"找不到消息号 {message_no} 的通知")
        (message_id, exchange_id, m_no, doc_no, doc_date, su_code, by_code,
         src_file, sender, receiver, control) = msg
        line = conn.execute(
            "SELECT l.line_id, l.line_no, l.part_no, l.quantity_text, "
            "l.quantity_num, l.quantity_den, l.unit, p.package_code, "
            "p.parent_code, p.depth, p.label, l.lin_start, l.lin_end, "
            "l.qty_start, l.qty_end "
            "FROM desadv_line l JOIN desadv_package p "
            "ON l.package_id=p.package_id "
            "WHERE l.message_id=? AND l.line_no=?",
            (message_id, line_no)).fetchone()
        if line is None:
            raise KeyError(f"通知 {message_no} 内找不到行号 {line_no}")
        (line_id, l_no, part_no, qty_text, q_num, q_den, unit, pkg_code,
         parent_code, depth, label, lin_start, lin_end, qty_start,
         qty_end) = line
        path = _package_path(conn, message_id, pkg_code)
        notes = [dict(zip(["note_seq", "note_text", "start_byte", "end_byte"],
                          r)) for r in conn.execute(
            "SELECT note_seq, note_text, start_byte, end_byte FROM desadv_ftx "
            "WHERE line_id=? ORDER BY note_seq", (line_id,))]
        return {
            "source_file": src_file,
            "exchange": {"exchange_id": exchange_id, "sender": sender,
                         "receiver": receiver, "exchange_control": control},
            "message": {"message_id": message_id, "message_no": m_no,
                        "doc_no": doc_no, "doc_date": doc_date,
                        "su_code": su_code, "by_code": by_code},
            "line_no": l_no,
            "part_no": part_no,
            "quantity_text": qty_text,
            "quantity_fraction": f"{q_num}/{q_den}",
            "unit": unit,
            "package_code": pkg_code,
            "package_label": label,
            "package_path": path,
            "notes": notes,
            "source_offsets": {
                "lin": [lin_start, lin_end],
                "qty": [qty_start, qty_end],
            },
        }
    finally:
        conn.close()


def _package_path(conn: sqlite3.Connection, message_id: int,
                  package_code: str) -> List[dict]:
    """沿 parent_code 向上取根→当前包装的路径（父项后到也已在落库前校验）。"""
    chain: List[dict] = []
    current = package_code
    seen: set[str] = set()
    while current is not None:
        if current in seen:  # 落库前已查环，这里仅作防御
            raise ValueError(f"包装层级存在环：{current}")
        seen.add(current)
        row = conn.execute(
            "SELECT package_code, parent_code, depth, pieces, package_type, "
            "label, cps_start, cps_end FROM desadv_package "
            "WHERE message_id=? AND package_code=?",
            (message_id, current)).fetchone()
        if row is None:
            raise KeyError(f"找不到包装 {current}")
        chain.append({
            "package_code": row[0], "parent_code": row[1], "depth": row[2],
            "pieces": row[3], "package_type": row[4], "label": row[5],
            "cps_span": [row[6], row[7]],
        })
        current = row[1]
    chain.reverse()
    return chain


def subtree_summary(db_path: str | Path, message_no: str,
                    package_code: str) -> dict:
    """任意包装的子树汇总：按零件号+单位累加该包装及全部后代的发货数量。"""
    conn = open_store(db_path)
    try:
        msg = conn.execute(
            "SELECT message_id FROM desadv_message WHERE message_no=?",
            (message_no,)).fetchone()
        if msg is None:
            raise KeyError(f"找不到消息号 {message_no} 的通知")
        message_id = msg[0]
        root = conn.execute(
            "SELECT package_code, parent_code, depth FROM desadv_package "
            "WHERE message_id=? AND package_code=?",
            (message_id, package_code)).fetchone()
        if root is None:
            raise KeyError(f"通知 {message_no} 内找不到包装 {package_code}")
        # 递归 CTE 取子树全部包装（含自身）
        descendants = [r[0] for r in conn.execute(
            "WITH RECURSIVE sub(package_code) AS ("
            "  SELECT package_code FROM desadv_package "
            "  WHERE message_id=? AND package_code=? "
            "  UNION ALL "
            "  SELECT p.package_code FROM desadv_package p "
            "  JOIN sub ON p.parent_code=sub.package_code "
            "  WHERE p.message_id=?) "
            "SELECT package_code FROM sub",
            (message_id, package_code, message_id))]
        placeholders = ",".join("?" for _ in descendants)
        rows = conn.execute(
            f"SELECT part_no, unit, quantity_num, quantity_den, "
            f"quantity_text FROM desadv_line WHERE message_id=? "
            f"AND package_id IN (SELECT package_id FROM desadv_package "
            f"WHERE message_id=? AND package_code IN ({placeholders}))",
            (message_id, message_id, *descendants)).fetchall()
        totals: Dict[tuple[str, str], Fraction] = {}
        line_count = 0
        for part_no, unit, num, den, _text in rows:
            totals[(part_no, unit)] = totals.get(
                (part_no, unit), Fraction(0)) + Fraction(num, den)
            line_count += 1
        summary = [
            {"part_no": part_no, "unit": unit,
             "quantity_text": _fraction_to_text(value),
             "quantity_fraction": f"{value.numerator}/{value.denominator}"}
            for (part_no, unit), value in sorted(totals.items())
        ]
        return {
            "message_no": message_no,
            "root_package": package_code,
            "subtree_package_count": len(descendants),
            "subtree_line_count": line_count,
            "totals": summary,
        }
    finally:
        conn.close()


def _fraction_to_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    from .parser import _decimal_from_fraction
    return _decimal_from_fraction(value)


def verify_slice(db_path: str | Path, source_file: str | Path,
                 start: int, end: int) -> bytes:
    """用库存的字节偏移直接从原文件切回未经改写的原始段。"""
    with open(source_file, "rb") as handle:
        handle.seek(start)
        return handle.read(end - start)
