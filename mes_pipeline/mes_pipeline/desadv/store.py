"""SQLite 关系表落库：schema、交换事务、重传识别与冲突保护。

表结构（业务表均带完整来源：文件、交换、消息、段序号、字节偏移）：

- ``desadv_exchange`` 交换（UNB/UNZ 身份、UNA、小数符、载荷哈希、字节跨度）
- ``desadv_message``  通知（UNH/BGM/DTM/两个 NAD 的解析结果）
- ``desadv_segment``  全部原始段（含原始字节 BLOB，偏移可直接切回原文）
- ``desadv_package``  包装层级（CPS/PAC/GIN，父子关系、深度、标签号）
- ``desadv_line``     发货行（LIN/QTY，精确十进制文本 + 分子/分母）
- ``desadv_ftx``      发货行备注（FTX，保留次序与原文）

事务边界为**单个交换**：一个交换的 UNZ、消息计数、层级与字段全部通过后才
COMMIT；任何失败 ROLLBACK，库内不留半条通知。重传按
（发送方, 接收方, 交换号）识别：载荷字节相同则跳过，不同则抛
``DesadvConflict`` 且旧资料不动。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional

from .errors import DesadvConflict, DesadvError
from .parser import (
    ExchangeHeader,
    LineRecord,
    MessageHeader,
    NoteRecord,
    PackageRecord,
)
from .tokenizer import Segment

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS desadv_exchange (
    exchange_id        INTEGER PRIMARY KEY,
    source_file        TEXT NOT NULL,
    sender             TEXT NOT NULL,
    receiver           TEXT NOT NULL,
    exchange_control   TEXT NOT NULL,
    unb_date           TEXT NOT NULL,
    unb_time           TEXT NOT NULL,
    has_una            INTEGER NOT NULL,
    una_start          INTEGER,
    una_end            INTEGER,
    decimal_char       TEXT NOT NULL,
    exchange_start     INTEGER NOT NULL,
    exchange_end       INTEGER NOT NULL,
    payload_hash       TEXT NOT NULL,
    seq_in_file        INTEGER NOT NULL,
    UNIQUE (sender, receiver, exchange_control)
);

CREATE TABLE IF NOT EXISTS desadv_message (
    message_id         INTEGER PRIMARY KEY,
    exchange_id        INTEGER NOT NULL REFERENCES desadv_exchange(exchange_id),
    seq_in_exchange    INTEGER NOT NULL,
    message_no         TEXT NOT NULL,
    doc_no             TEXT NOT NULL,
    doc_date           TEXT NOT NULL,
    su_code            TEXT NOT NULL,
    by_code            TEXT NOT NULL,
    UNIQUE (exchange_id, message_no),
    UNIQUE (exchange_id, doc_no)
);

CREATE TABLE IF NOT EXISTS desadv_segment (
    segment_id         INTEGER PRIMARY KEY,
    exchange_id        INTEGER NOT NULL REFERENCES desadv_exchange(exchange_id),
    message_id         INTEGER REFERENCES desadv_message(message_id),
    segment_tag        TEXT NOT NULL,
    seq_in_exchange    INTEGER NOT NULL,
    seq_in_message     INTEGER,
    start_byte         INTEGER NOT NULL,
    end_byte           INTEGER NOT NULL,
    raw_segment        BLOB NOT NULL,
    UNIQUE (exchange_id, seq_in_exchange)
);

CREATE TABLE IF NOT EXISTS desadv_package (
    package_id         INTEGER PRIMARY KEY,
    message_id         INTEGER NOT NULL REFERENCES desadv_message(message_id),
    package_code       TEXT NOT NULL,
    parent_code        TEXT,
    depth              INTEGER NOT NULL,
    pieces             INTEGER NOT NULL,
    package_type       TEXT NOT NULL,
    label              TEXT,
    cps_start          INTEGER NOT NULL,
    cps_end            INTEGER NOT NULL,
    UNIQUE (message_id, package_code),
    UNIQUE (message_id, label)
);

CREATE TABLE IF NOT EXISTS desadv_line (
    line_id            INTEGER PRIMARY KEY,
    message_id         INTEGER NOT NULL REFERENCES desadv_message(message_id),
    package_id         INTEGER NOT NULL REFERENCES desadv_package(package_id),
    line_no            TEXT NOT NULL,
    part_no            TEXT NOT NULL,
    quantity_text      TEXT NOT NULL,
    quantity_num       INTEGER NOT NULL,
    quantity_den       INTEGER NOT NULL,
    unit               TEXT NOT NULL CHECK (unit IN ('PCE', 'KGM')),
    lin_start          INTEGER NOT NULL,
    lin_end            INTEGER NOT NULL,
    qty_start          INTEGER NOT NULL,
    qty_end            INTEGER NOT NULL,
    UNIQUE (message_id, line_no)
);

CREATE TABLE IF NOT EXISTS desadv_ftx (
    ftx_id             INTEGER PRIMARY KEY,
    line_id            INTEGER NOT NULL REFERENCES desadv_line(line_id),
    note_seq           INTEGER NOT NULL,
    note_text          TEXT NOT NULL,
    start_byte         INTEGER NOT NULL,
    end_byte           INTEGER NOT NULL,
    UNIQUE (line_id, note_seq)
);

CREATE INDEX IF NOT EXISTS idx_pkg_parent
    ON desadv_package(message_id, parent_code);
CREATE INDEX IF NOT EXISTS idx_line_part
    ON desadv_line(message_id, part_no, unit);
"""


def open_store(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）DESADV 库并确保 schema 存在。"""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    return conn


class SqliteSink:
    """解析器的 SQLite 落库端口；配合显式 BEGIN/COMMIT/ROLLBACK 使用。"""

    def __init__(self, conn: sqlite3.Connection, source_file: str,
                 seq_in_file: int) -> None:
        self.conn = conn
        self.source_file = source_file
        self.seq_in_file = seq_in_file
        self.exchange_row_id: Optional[int] = None
        self._message_ids: List[int] = []
        self._package_row_ids: dict[tuple[int, str], int] = {}
        self._line_row_ids: dict[tuple[int, str], int] = {}

    # ------------------------------------------------------------------ #
    def attach_exchange(self, header: ExchangeHeader) -> int:
        """解析阶段登记交换头；真实交换行在 service 层判定非重传后插入。

        返回一个进程内逻辑 id（负数占位），供消息/段行挂接；事务结束时
        service 调用 ``register_exchange_row`` 替换为真实 rowid。
        """
        self._pending_header = header
        self._logical_id = -self.seq_in_file
        self.exchange_row_id = self._logical_id
        return self._logical_id

    def register_exchange_row(self, payload_hash: str,
                              exchange_end: int) -> int:
        """重传判定通过后插入真实交换行，并把挂接的子行改写为真实 rowid。"""
        header = self._pending_header
        cur = self.conn.execute(
            "INSERT INTO desadv_exchange (source_file, sender, receiver, "
            "exchange_control, unb_date, unb_time, has_una, una_start, una_end, "
            "decimal_char, exchange_start, exchange_end, payload_hash, "
            "seq_in_file) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.source_file, header.sender, header.receiver, header.control,
             header.date_ymd, header.time_hhmm, 1 if header.has_una else 0,
             header.una_start, header.una_end, header.decimal_char,
             header.start, exchange_end, payload_hash, self.seq_in_file))
        real_id = int(cur.lastrowid)
        for table in ("desadv_segment", "desadv_message"):
            self.conn.execute(
                f"UPDATE {table} SET exchange_id=? WHERE exchange_id=?",
                (real_id, self._logical_id))
        self.exchange_row_id = real_id
        return real_id

    def finish_exchange(self, payload_hash: str, exchange_end: int) -> None:
        """交换解析成功后补哈希/结束偏移（兼容旧调用点）。"""
        self.conn.execute(
            "UPDATE desadv_exchange SET payload_hash=?, exchange_end=? "
            "WHERE exchange_id=?",
            (payload_hash, exchange_end, self.exchange_row_id))

    # ------------------------------------------------------------------ #
    def begin_message(self, exchange_id: int, seq_in_exchange: int,
                      header: MessageHeader) -> int:
        cur = self.conn.execute(
            "INSERT INTO desadv_message (exchange_id, seq_in_exchange, "
            "message_no, doc_no, doc_date, su_code, by_code) "
            "VALUES (?,?,?,?,?,?,?)",
            (exchange_id, seq_in_exchange, header.message_no,
             header.doc_no, header.doc_date, header.su_code, header.by_code))
        message_id = int(cur.lastrowid)
        self._message_ids.append(message_id)
        return message_id

    def update_message_header(self, message_id: int, *, doc_no: str,
                              doc_date: str, su_code: str, by_code: str,
                              partial: bool) -> None:
        self.conn.execute(
            "UPDATE desadv_message SET doc_no=?, doc_date=?, su_code=?, "
            "by_code=? WHERE message_id=?",
            (doc_no, doc_date, su_code, by_code, message_id))

    def add_segment(self, exchange_id: int, message_id: Optional[int],
                    segment: Segment, seq_in_message: Optional[int],
                    seq_in_exchange: int) -> None:
        self.conn.execute(
            "INSERT INTO desadv_segment (exchange_id, message_id, segment_tag, "
            "seq_in_exchange, seq_in_message, start_byte, end_byte, "
            "raw_segment) VALUES (?,?,?,?,?,?,?,?)",
            (exchange_id, message_id, segment.tag, seq_in_exchange,
             seq_in_message, segment.start, segment.end,
             sqlite3.Binary(segment.raw)))

    def add_package(self, message_id: int, record: PackageRecord) -> None:
        cur = self.conn.execute(
            "INSERT INTO desadv_package (message_id, package_code, parent_code, "
            "depth, pieces, package_type, label, cps_start, cps_end) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (message_id, record.package_id, record.parent_id, record.depth,
             record.pieces, record.package_type, record.label,
             record.cps_start, record.cps_end))
        self._package_row_ids[(message_id, record.package_id)] = int(
            cur.lastrowid)

    def upsert_package_fields(self, message_id: int, package_code: str,
                              **fields) -> None:
        """CPS/PAC/GIN 增量补写包装字段（pieces/package_type/label）。"""
        if not fields:
            return
        columns = ", ".join(f"{key}=?" for key in fields)
        params = list(fields.values()) + [message_id, package_code]
        self.conn.execute(
            f"UPDATE desadv_package SET {columns} "
            "WHERE message_id=? AND package_code=?", params)

    def update_package_depth(self, message_id: int, package_code: str,
                             depth: int, cps_end: int) -> None:
        """UNT 图校验通过后回写深度与组结束偏移。"""
        self.conn.execute(
            "UPDATE desadv_package SET depth=?, cps_end=? "
            "WHERE message_id=? AND package_code=?",
            (depth, cps_end, message_id, package_code))

    def add_line(self, message_id: int, record: LineRecord) -> None:
        package_row = self._package_row_ids.get(
            (message_id, record.package_id))
        if package_row is None:
            # 包在 UNT 时统一落库；行先到时先按 code 查一次（正常顺序下不会发生）
            row = self.conn.execute(
                "SELECT package_id FROM desadv_package WHERE message_id=? "
                "AND package_code=?", (message_id, record.package_id)).fetchone()
            if row is None:
                raise DesadvError(
                    f"发货行 {record.line_no} 找不到所属包装 {record.package_id}")
            package_row = row[0]
        cur = self.conn.execute(
            "INSERT INTO desadv_line (message_id, package_id, line_no, part_no, "
            "quantity_text, quantity_num, quantity_den, unit, lin_start, "
            "lin_end, qty_start, qty_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, package_row, record.line_no, record.part_no,
             record.quantity_text, record.quantity.numerator,
             record.quantity.denominator, record.unit,
             record.lin_start, record.lin_end, record.qty_start,
             record.qty_end))
        self._line_row_ids[(message_id, record.line_no)] = int(cur.lastrowid)

    def add_note(self, message_id: int, record: NoteRecord) -> None:
        line_row = self._line_row_ids.get((message_id, record.line_no))
        if line_row is None:
            row = self.conn.execute(
                "SELECT line_id FROM desadv_line WHERE message_id=? AND line_no=?",
                (message_id, record.line_no)).fetchone()
            if row is None:
                raise DesadvError(
                    f"备注找不到所属发货行 {record.line_no}")
            line_row = row[0]
        self.conn.execute(
            "INSERT INTO desadv_ftx (line_id, note_seq, note_text, "
            "start_byte, end_byte) VALUES (?,?,?,?,?)",
            (line_row, record.seq, record.text, record.start, record.end))
