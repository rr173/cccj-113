"""DESADV 接入器独立复核脚本（不调用生产查询函数，独立重算并逐项核对）。

复核内容：

1. 块大小不变性：同一份材料用 1/2/7/4096 字节块导入，业务表与来源偏移完全相同；
2. 原文切片：用库存字节偏移切回的段与库存原始 BLOB 逐字节一致；
3. 子树汇总：用递归 CTE 之外的纯 Python 遍历独立重算，核对 PCE/KGM 分组；
4. 故障语义：坏交换整体回滚、重传不翻倍、同号改文冲突保留旧资料。

用法：
    python -m desadv_demo.verify --materials desadv_demo/materials --work /tmp/dv
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from fractions import Fraction
from pathlib import Path

from mes_pipeline.desadv.errors import DesadvConflict, DesadvError
from mes_pipeline.desadv.service import import_file

BLOCK_SIZES = (1, 2, 7, 4096)
TABLES = ("desadv_exchange", "desadv_message", "desadv_segment",
          "desadv_package", "desadv_line", "desadv_ftx")
SKIP_COLS = {"exchange_id", "message_id", "segment_id", "package_id",
             "line_id", "ftx_id", "source_file"}


def _snapshot(db: Path):
    conn = sqlite3.connect(db)
    out = []
    for table in TABLES:
        cols = [r[1] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall() if r[1] not in SKIP_COLS]
        select = ",".join(cols)
        out.append((table, conn.execute(
            f"SELECT {select} FROM {table} ORDER BY 1").fetchall()))
    conn.close()
    return out


def verify_block_invariance(materials: Path, work: Path) -> None:
    """1/2/7/4096 块大小导入同一材料，业务表与偏移必须完全一致。"""
    data_path = materials / "01_multi.edi"
    snaps = {}
    for bs in BLOCK_SIZES:
        db = work / f"inv_{bs}.db"
        import_file(db, data_path, block_size=bs)
        snaps[bs] = _snapshot(db)
    baseline = snaps[BLOCK_SIZES[0]]
    for bs in BLOCK_SIZES[1:]:
        assert snaps[bs] == baseline, f"块大小 {bs} 与基线结果不一致"
    print(f"[OK] 块大小不变性：{BLOCK_SIZES} 字节块导入结果逐表一致")


def verify_raw_slices(materials: Path, work: Path) -> None:
    """库存偏移切回原文，与库存 BLOB 逐字节相等。"""
    data_path = materials / "01_multi.edi"
    db = work / "slice.db"
    import_file(db, data_path, block_size=3)
    raw = data_path.read_bytes()
    conn = sqlite3.connect(db)
    n = 0
    for start, end, blob in conn.execute(
            "SELECT start_byte, end_byte, raw_segment FROM desadv_segment"):
        assert raw[start:end] == blob, f"切片不一致 [{start},{end})"
        n += 1
    conn.close()
    print(f"[OK] 原文切片：{n} 个段均可用字节偏移逐字节切回")


def verify_subtree(materials: Path, work: Path) -> None:
    """独立用 Python 遍历包装树重算子树，与库存发货行核对。"""
    db = work / "sub.db"
    import_file(db, materials / "01_multi.edi", block_size=7)
    conn = sqlite3.connect(db)
    # 取 MSG-1 的包装父子关系
    msg_id = conn.execute(
        "SELECT message_id FROM desadv_message WHERE message_no='MSG-1'"
    ).fetchone()[0]
    parent = dict(conn.execute(
        "SELECT package_code, parent_code FROM desadv_package "
        "WHERE message_id=?", (msg_id,)).fetchall())
    children: dict[str, list[str]] = {}
    for code, par in parent.items():
        children.setdefault(par, []).append(code)

    def descendants(root: str):
        seen = []
        stack = [root]
        while stack:
            node = stack.pop()
            seen.append(node)
            stack.extend(children.get(node, []))
        return seen

    # 手工核算 P1 子树（P1 后到，含 B1/B2）
    sub = set(descendants("P1"))
    rows = conn.execute(
        "SELECT p.package_code, l.part_no, l.unit, l.quantity_num, "
        "l.quantity_den FROM desadv_line l JOIN desadv_package p "
        "ON l.package_id=p.package_id WHERE p.message_id=?",
        (msg_id,)).fetchall()
    expect: dict[tuple[str, str], Fraction] = {}
    for pkg, part, unit, num, den in rows:
        if pkg in sub:
            expect[(part, unit)] = expect.get((part, unit), Fraction(0)) \
                + Fraction(num, den)
    # 期望值（按生成材料手工核算）：
    # B1: PART-阿尔法 3.5 PCE；PART-A 2.5 KGM
    # B2: PART-A 10 PCE
    assert expect[("PART-阿尔法", "PCE")] == Fraction(35, 10)
    assert expect[("PART-A", "KGM")] == Fraction(25, 10)
    assert expect[("PART-A", "PCE")] == Fraction(10)
    # 单位分组互不相加
    assert ("PART-A", "PCE") in expect and ("PART-A", "KGM") in expect
    print("[OK] 子树汇总：P1 手工核算 "
          "PART-阿尔法=3.5 PCE、PART-A=2.5 KGM、PART-A=10 PCE，单位分别汇总")
    conn.close()


def verify_failure_and_recovery(materials: Path, work: Path) -> None:
    """坏交换回滚、前交换存活；重传不翻倍；同号改文冲突。"""
    db = work / "fail.db"
    # 前好后坏：仅好交换存活
    try:
        import_file(db, materials / "recover_two_exchanges.edi", block_size=3)
        raise AssertionError("坏交换本应被拒绝")
    except DesadvError:
        pass
    conn = sqlite3.connect(db)
    assert [r[0] for r in conn.execute(
        "SELECT exchange_control FROM desadv_exchange")] == ["EX-GOOD"]
    conn.close()

    # 再传一次：好交换识别为重复，坏交换仍拒绝，行数不翻倍
    try:
        import_file(db, materials / "recover_two_exchanges.edi", block_size=3)
    except DesadvError:
        pass
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT count(*) FROM desadv_exchange").fetchone()[0] == 1
    conn.close()

    # 同号改文冲突
    base = work / "base.db"
    import_file(base, materials / "01_multi.edi", block_size=4096)
    try:
        import_file(base, materials / "conflict_same_id.edi", block_size=1)
        raise AssertionError("同号改文本应冲突")
    except DesadvConflict:
        pass
    conn = sqlite3.connect(base)
    assert conn.execute(
        "SELECT count(*) FROM desadv_exchange").fetchone()[0] == 2
    assert conn.execute(
        "SELECT doc_no FROM desadv_message WHERE message_no='MSG-1'"
    ).fetchone()[0] == "DN-2026-001"      # 旧资料保持不动
    conn.close()
    print("[OK] 故障恢复：坏交换回滚、重传不翻倍、同号改文冲突且旧资料不动")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DESADV 独立复核")
    parser.add_argument("--materials", required=True)
    parser.add_argument("--work", required=True)
    args = parser.parse_args(argv)
    materials = Path(args.materials)
    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    verify_block_invariance(materials, work)
    verify_raw_slices(materials, work)
    verify_subtree(materials, work)
    verify_failure_and_recovery(materials, work)
    print("全部独立复核通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
