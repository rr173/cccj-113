"""导入编排：有界块流式读取、逐交换事务、重传/冲突与故障注入。

关键保证：

- 输入文件始终以二进制、有界字节块读取（``block_size`` 可调），从不整文件
  read/split；
- **交换是事务边界**：UNZ、全部消息计数、层级与字段校验都通过后才 COMMIT；
  截断/校验失败/提交前退出都会 ROLLBACK（或进程被杀，由 SQLite 回滚日志
  保证原子性），不留半条通知；
- 同身份（发送方、接收方、交换号）重传：载荷字节完全相同则跳过、不新增
  业务行；字节不同抛 ``DesadvConflict``，旧资料保持不动；
- 一个文件串接多个交换时，前面已提交的交换不受后面坏交换影响，下次重传
  也不会让已提交交换翻倍。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .errors import DesadvConflict, DesadvError
from .parser import ExchangeParser, _check_unb_segment
from .store import SqliteSink, open_store
from .tokenizer import SegmentStreamer

#: 单段长度默认上限（字节），可在命令行调整
DEFAULT_MAX_SEGMENT_BYTES = 1_048_576


@dataclass
class _HeadWithUnb:
    """预读 UNB 的结果：交换头值对象 + 原始 UNB 段（供解析器重放）。"""

    header: object
    unb_segment: object

    # 便于 service 直接取头字段
    @property
    def sender(self) -> str:
        return self.header.sender

    @property
    def receiver(self) -> str:
        return self.header.receiver

    @property
    def control(self) -> str:
        return self.header.control

    @property
    def start(self) -> int:
        return self.header.start

    @property
    def end(self) -> int:
        return self.header.end


@dataclass
class ImportedExchange:
    seq_in_file: int
    sender: str
    receiver: str
    control: str
    payload_hash: str
    outcome: str               # "inserted" | "duplicate"
    exchange_start: int
    exchange_end: int
    message_count: int
    line_count: int


@dataclass
class ImportResult:
    db_path: str
    source_file: str
    block_size: int
    exchanges: List[ImportedExchange]
    stopped_at: Optional[int] = None      # 坏交换截断时的文件字节位置

    @property
    def total_lines(self) -> int:
        return sum(e.line_count for e in self.exchanges)


def import_file(db_path: str | Path, source_file: str | Path, *,
                block_size: int = 65536,
                max_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES,
                fault_inject_before_commit: bool = False) -> ImportResult:
    """流式导入一个 DESADV 文件，返回每个交换的处理结果。

    fault_inject_before_commit 为 True 时，在**第一个**交换全部校验通过、
    即将 COMMIT 前以退出码 11 杀掉进程（可重复的提交前故障注入）。
    """
    source_path = Path(source_file)
    conn = open_store(db_path)
    results: List[ImportedExchange] = []
    try:
        with open(source_path, "rb") as handle:
            streamer = SegmentStreamer(
                handle.read, block_size=block_size,
                max_segment_bytes=max_segment_bytes)
            seq_in_file = 0
            while True:
                started = streamer.file_offset
                begin = streamer.begin_exchange()
                if begin is None:
                    break
                chars, una_span = begin
                seq_in_file += 1
                outcome = _process_exchange(
                    conn, streamer, chars, una_span, seq_in_file,
                    str(source_path), fault_inject_before_commit)
                results.append(outcome)
    finally:
        conn.close()
    return ImportResult(
        db_path=str(db_path), source_file=str(source_path),
        block_size=block_size, exchanges=results)


def _process_exchange(conn: sqlite3.Connection, streamer: SegmentStreamer,
                      chars, una_span, seq_in_file: int,
                      source_file: str,
                      fault_inject: bool) -> ImportedExchange:
    # 先只读到 UNB 以识别交换身份（发送方/接收方/交换号），再决定重传策略
    head = _read_unb_header(streamer, chars, una_span, seq_in_file)
    existing = conn.execute(
        "SELECT exchange_id, payload_hash, source_file FROM desadv_exchange "
        "WHERE sender=? AND receiver=? AND exchange_control=?",
        (head.sender, head.receiver, head.control)).fetchone()

    # 显式事务：解析与落库全部成功后才 COMMIT，失败即 ROLLBACK。
    # 解析期交换行用负数逻辑 id 挂接子行，故事务内临时关闭外键强制，
    # 在 register_exchange_row 改写为真实 rowid 后、COMMIT 前再开启并
    # foreign_key_check 复核，保证提交的资料外键完整。
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        sink = SqliteSink(conn, source_file, seq_in_file)
        parser = ExchangeParser(sink, streamer, chars, seq_in_file,
                                una_span, pre_parsed_unb=(head.header,
                                                          head.unb_segment))
        parser.run()
        payload_hash = streamer.finish_hashing()
        exchange_end = streamer.file_offset
        if existing is not None:
            old_id, old_hash, old_source = existing
            if old_hash == payload_hash:
                conn.rollback()       # 完整重传：不新增任何业务行
                conn.execute("PRAGMA foreign_keys=ON")
                return _summarize_existing(conn, old_id, seq_in_file,
                                           head.header, payload_hash,
                                           exchange_end, "duplicate")
            conn.rollback()
            conn.execute("PRAGMA foreign_keys=ON")
            raise DesadvConflict(
                f"交换身份（发送方={head.sender} 接收方={head.receiver} "
                f"交换号={head.control}）已存在于 {old_source}，但载荷字节"
                f"不同（旧哈希 {old_hash[:12]}… / 新哈希 "
                f"{payload_hash[:12]}…）；本次拒绝并保留旧资料",
                byte_offset=head.start, end_byte_offset=head.end,
                segment_tag="UNB", exchange_seq=seq_in_file)
        exchange_id = sink.register_exchange_row(payload_hash, exchange_end)
        conn.execute("PRAGMA foreign_keys=ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise DesadvError(
                f"外键完整性检查失败：{violations[:3]}",
                exchange_seq=seq_in_file)
        if fault_inject:
            # 全部校验已通过、数据已写入但尚未 COMMIT：模拟提交前进程退出
            os._exit(11)
        conn.commit()
    except DesadvError:
        conn.rollback()
        conn.execute("PRAGMA foreign_keys=ON")
        raise
    except Exception:
        conn.rollback()
        conn.execute("PRAGMA foreign_keys=ON")
        raise
    return _summarize_existing(conn, exchange_id, seq_in_file, head.header,
                               payload_hash, exchange_end, "inserted")


def _summarize_existing(conn: sqlite3.Connection, exchange_id: int,
                        seq_in_file: int, header, payload_hash: str,
                        exchange_end: int, outcome: str) -> ImportedExchange:
    line_count = conn.execute(
        "SELECT COUNT(*) FROM desadv_line l JOIN desadv_message m "
        "ON l.message_id=m.message_id WHERE m.exchange_id=?",
        (exchange_id,)).fetchone()[0]
    message_count = conn.execute(
        "SELECT COUNT(*) FROM desadv_message WHERE exchange_id=?",
        (exchange_id,)).fetchone()[0]
    return ImportedExchange(
        seq_in_file=seq_in_file, sender=header.sender,
        receiver=header.receiver, control=header.control,
        payload_hash=payload_hash, outcome=outcome,
        exchange_start=header.start, exchange_end=exchange_end,
        message_count=message_count, line_count=line_count)


def _read_unb_header(streamer: SegmentStreamer, chars, una_span,
                     exchange_seq: int) -> _HeadWithUnb:
    """只解析 UNB 一段，产出交换头；段对象保留供解析器重放。"""
    segment = streamer.next_segment()
    if segment is None or segment.tag != "UNB":
        raise DesadvError("交换必须以 UNB 开始",
                          byte_offset=streamer.file_offset,
                          exchange_seq=exchange_seq)
    header = _check_unb_segment(segment, chars, una_span)
    return _HeadWithUnb(header=header, unb_segment=segment)
