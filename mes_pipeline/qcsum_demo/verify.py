"""质检汇总独立核对程序：不调用生产查询函数，直接读库逐项核对。

核对方式：用普通 sqlite3 连接独立打开库文件，直接读 qc_summary 持久汇总，
并与 lots/samples 上的完整 LEFT JOIN/GROUP BY 参考查询（reference.py，
不进生产增量路径）逐组对比，包括“最后一个批次离组后整组消失”的空组情形。

用法：
    python -m qcsum_demo.verify --materials qcsum_demo/materials --work /tmp/qcsv
    python -m qcsum_demo.verify --db /path/to/qcsum.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from mes_pipeline.qcsum.reference import REFERENCE_SQL, SUMMARY_COLUMNS, diff_summary
from mes_pipeline.qcsum.store import SummaryStore

_SUMMARY_SELECT = (
    "SELECT supplier, site, joined_rows, n_samples, n_lots, n_grades, rejected_sum"
    " FROM qc_summary ORDER BY supplier, site"
)


def _read_persistent_summary(db_path):
    """独立连接直接读持久汇总表（不走 SummaryStore.summary）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(_SUMMARY_SELECT)
        return [dict(zip(SUMMARY_COLUMNS, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _read_reference(db_path):
    """独立连接在原表上跑完整 LEFT JOIN/GROUP BY 参考查询。"""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(REFERENCE_SQL)
        return [dict(zip(SUMMARY_COLUMNS, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def verify_db(db_path) -> int:
    expected = _read_reference(db_path)
    actual = _read_persistent_summary(db_path)
    diffs = diff_summary(expected, actual)
    if diffs:
        print(f"FAIL {db_path}: 持久汇总与参考查询不一致")
        for d in diffs:
            print(f"  {d}")
        return 1
    print(f"OK {db_path}: {len(actual)} 个分组与参考查询一致（含空组情形）")
    return 0


def verify_steps(materials: Path, work: Path) -> int:
    """重放演示材料：每批应用后核对持久汇总与参考查询。"""
    manifest = json.loads((materials / "manifest.json").read_text(encoding="utf-8"))
    work.mkdir(parents=True, exist_ok=True)
    failures = 0
    for sc in manifest["scenarios"]:
        db = work / f"verify_{sc['scenario']}.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        store = SummaryStore(db)
        try:
            for step in sc["steps"]:
                batch_text = (materials / step["file"]).read_text(encoding="utf-8")
                store.apply_batch(batch_text)  # 生产写入路径（被核对对象）
                store.close()  # 关掉再独立打开，模拟另一进程读取
                expected = _read_reference(db)
                actual = _read_persistent_summary(db)
                diffs = diff_summary(expected, actual)
                tag = "OK  " if not diffs else "FAIL"
                print(f"{tag} [{sc['scenario']}] {step['step']}")
                if diffs:
                    failures += 1
                    for d in diffs:
                        print(f"      {d}")
                store = SummaryStore(db)
        finally:
            store.close()
    total = sum(len(s["steps"]) for s in manifest["scenarios"])
    print(f"逐步核对结束：{total - failures}/{total} 步一致")
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="质检汇总独立核对程序")
    parser.add_argument("--materials", help="演示材料目录（generate --scenario demo）")
    parser.add_argument("--work", default="/tmp/qcsum_verify")
    parser.add_argument("--db", help="直接核对一个已有库")
    args = parser.parse_args(argv)
    if args.db:
        return verify_db(args.db)
    if args.materials:
        return verify_steps(Path(args.materials), Path(args.work))
    parser.error("需要 --materials 或 --db")


if __name__ == "__main__":
    sys.exit(main())
