"""事务原子性测试：非法操作整体回滚、成功重送、改文冲突、两个真实终止点。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mes_pipeline.qcsum import QcsumConflict, QcsumValidation, SummaryStore
from mes_pipeline.qcsum.reference import diff_summary, reference_summary
from mes_pipeline.qcsum.store import CRASH_EXIT_CODE

REPO_ROOT = Path(__file__).resolve().parents[1]

TABLES = ("lots", "samples", "qc_sample_agg", "qc_sample_grade_agg",
          "qc_grade_counts", "qc_summary", "qc_batches")


def _snapshot(store):
    """全库快照（原表 + 辅助计数 + 汇总 + 成功账），用于回滚相等性断言。"""
    snap = {}
    for table in TABLES:
        rows = store.conn.execute(
            f"SELECT * FROM {table} ORDER BY 1").fetchall()
        snap[table] = [tuple(r) for r in rows]
    return snap


def _lot(lot_id="L1", supplier="甲", site="S1"):
    return {"lot_id": lot_id, "supplier": supplier, "site": site}


def _sample(sample_id="S1", lot_id="L1", grade="g", rejected=0):
    return {"sample_id": sample_id, "lot_id": lot_id,
            "grade": grade, "rejected_units": rejected}


def _ins(table, row):
    key = row["lot_id"] if table == "lots" else row["sample_id"]
    return {"op": "insert", "table": table, "key": key, "row": row}


def _del(table, key):
    return {"op": "delete", "table": table, "key": key}


def _run_cli(args, cwd):
    """从源码目录外以子进程调用 CLI（真实进程，真实终止）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-P", "-m", "mes_pipeline.qcsum.cli", *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


class RollbackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = f"{self._tmp.name}/t.db"
        self.store = SummaryStore(self.db)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_illegal_op_rolls_back_everything_and_batch_id_reusable(self):
        self.store.apply_batch({"batch_id": "B0", "ops": [
            _ins("lots", _lot("L0")), _ins("samples", _sample("S0", "L0", "g", 2))]})
        before = _snapshot(self.store)
        bad_batch = {"batch_id": "B1", "ops": [
            _ins("lots", _lot("L1", "乙", "S2")),
            _ins("samples", _sample("S1", "L1", "x", 5)),
            _del("samples", "NOPE"),  # 不存在 → 整批失败
        ]}
        with self.assertRaises(QcsumValidation):
            self.store.apply_batch(bad_batch)
        # 原表、辅助计数、汇总、成功账全部保持批前状态
        self.assertEqual(_snapshot(self.store), before)
        # 修正后同一 batch_id 可以继续使用
        fixed = {"batch_id": "B1", "ops": [
            _ins("lots", _lot("L1", "乙", "S2")),
            _ins("samples", _sample("S1", "L1", "x", 5)),
        ]}
        result = self.store.apply_batch(fixed)
        self.assertEqual(result["status"], "applied")
        diffs = diff_summary(reference_summary(self.store.conn), self.store.summary())
        self.assertEqual(diffs, [])


class ReplayConflictTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = f"{self._tmp.name}/t.db"
        self.store = SummaryStore(self.db)
        self.batch = {"batch_id": "B1", "ops": [
            _ins("lots", _lot("L1")),
            _ins("samples", _sample("S1", "L1", "g", 3)),
            _ins("samples", _sample("S2", "L1", "h", None)),
        ]}

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_replay_returns_stored_result_without_double_apply(self):
        first = self.store.apply_batch(self.batch)
        after_first = _snapshot(self.store)
        second = self.store.apply_batch(self.batch)
        self.assertEqual(second["status"], "replayed")
        self.assertEqual(second["stats"], first["stats"])  # 既有结论原样返回
        self.assertEqual(_snapshot(self.store), after_first)  # 不重复加减
        self.assertEqual(len(self.store.batches()), 1)

    def test_key_order_is_not_content_change(self):
        self.store.apply_batch(self.batch)
        # 同一内容、对象键次序不同的原始 JSON → 重送，不是冲突
        text = ('{"ops": [{"row": {"site": "S1", "supplier": "甲", "lot_id": "L1"},'
                ' "key": "L1", "table": "lots", "op": "insert"},'
                ' {"row": {"rejected_units": 3, "grade": "g", "lot_id": "L1",'
                ' "sample_id": "S1"}, "key": "S1", "table": "samples",'
                ' "op": "insert"},'
                ' {"row": {"rejected_units": null, "grade": "h", "lot_id": "L1",'
                ' "sample_id": "S2"}, "key": "S2", "table": "samples",'
                ' "op": "insert"}], "batch_id": "B1"}')
        result = self.store.apply_batch(text)
        self.assertEqual(result["status"], "replayed")

    def test_same_id_different_content_conflicts(self):
        self.store.apply_batch(self.batch)
        changed = {"batch_id": "B1", "ops": [_ins("lots", _lot("L1", "乙"))]}
        with self.assertRaises(QcsumConflict):
            self.store.apply_batch(changed)

    def test_op_order_is_meaningful(self):
        self.store.apply_batch({"batch_id": "B2", "ops": [
            _ins("lots", _lot("L1")), _ins("lots", _lot("L2", "乙"))]})
        reordered = {"batch_id": "B2", "ops": [
            _ins("lots", _lot("L2", "乙")), _ins("lots", _lot("L1"))]}
        with self.assertRaises(QcsumConflict):
            self.store.apply_batch(reordered)


class CrashPointTest(unittest.TestCase):
    """两个真实终止点：进程被 os._exit 杀死，不改库内容伪装崩溃。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = self._tmp.name
        self.db = f"{self.work}/crash.db"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_batch(self, name, batch):
        path = f"{self.work}/{name}.json"
        Path(path).write_text(json.dumps(batch, ensure_ascii=False),
                              encoding="utf-8")
        return path

    def _apply_cli(self, batch_path, crash_at=None):
        args = ["apply", self.db, batch_path]
        if crash_at:
            args += ["--crash-at", crash_at]
        return _run_cli(args, cwd=self.work)

    def test_crash_after_base_leaves_complete_old_state(self):
        b1 = self._write_batch("b1", {"batch_id": "B1", "ops": [
            _ins("lots", _lot("L1")), _ins("samples", _sample("S1", "L1", "g", 3))]})
        proc = self._apply_cli(b1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with SummaryStore(self.db) as store:
            before = _snapshot(store)

        b2 = self._write_batch("b2", {"batch_id": "B2", "ops": [
            _ins("lots", _lot("L2", "乙", "S2")),
            _ins("samples", _sample("S2", "L2", "x", 5))]})
        proc = self._apply_cli(b2, crash_at="after_base")
        self.assertEqual(proc.returncode, CRASH_EXIT_CODE)  # 进程真实终止

        # 新进程打开：只能看见完整旧状态（事务未提交，全部回滚）
        with SummaryStore(self.db) as store:
            self.assertEqual(_snapshot(store), before)
            self.assertEqual([b["batch_id"] for b in store.batches()], ["B1"])
            # 重送该批：正常生效且只生效一次
            result = store.apply_batch(json.loads(Path(b2).read_text()))
            self.assertEqual(result["status"], "applied")
            diffs = diff_summary(reference_summary(store.conn), store.summary())
            self.assertEqual(diffs, [])
            self.assertEqual(len(store.batches()), 2)

    def test_crash_after_commit_shows_complete_new_state_and_replay_once(self):
        b1 = self._write_batch("b1", {"batch_id": "B1", "ops": [
            _ins("lots", _lot("L1")), _ins("samples", _sample("S1", "L1", "g", 3))]})
        proc = self._apply_cli(b1, crash_at="after_commit")
        self.assertEqual(proc.returncode, CRASH_EXIT_CODE)  # 提交后、返回前终止

        # 新进程打开：只能看见完整新状态
        with SummaryStore(self.db) as store:
            rows = store.summary()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["n_samples"], rows[0]["rejected_sum"]), (1, 3))
            self.assertEqual(len(store.batches()), 1)
            # 重送该批：只返回既有结论，不重复加减
            result = store.apply_batch(json.loads(Path(b1).read_text()))
            self.assertEqual(result["status"], "replayed")
            rows2 = store.summary()
            self.assertEqual(rows2, rows)
            self.assertEqual(len(store.batches()), 1)


if __name__ == "__main__":
    unittest.main()
