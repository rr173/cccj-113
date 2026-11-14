"""装载与读取测试：空库装载、非空拒绝、重复主键拒绝、装载后与参考查询一致。"""
from __future__ import annotations

import tempfile
import unittest

from mes_pipeline.qcsum import QcsumState, QcsumValidation, SummaryStore
from mes_pipeline.qcsum.datagen import generate_lots, generate_samples
from mes_pipeline.qcsum.reference import diff_summary, reference_summary


class LoadInitialTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = f"{self._tmp.name}/t.db"
        self.store = SummaryStore(self.db)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_load_and_summary_matches_reference(self):
        lots = [
            {"lot_id": "L1", "supplier": "甲", "site": "S1"},
            {"lot_id": "L2", "supplier": "甲", "site": "S1"},
            {"lot_id": "L3", "supplier": "乙", "site": "S2"},
        ]
        samples = [
            {"sample_id": "S1", "lot_id": "L1", "grade": "g", "rejected_units": 3},
            {"sample_id": "S2", "lot_id": "L1", "grade": None, "rejected_units": None},
            {"sample_id": "S3", "lot_id": "L1", "grade": "g", "rejected_units": 0},
            {"sample_id": "S4", "lot_id": "L3", "grade": "", "rejected_units": 7},
            {"sample_id": "S5", "lot_id": "L9", "grade": "o", "rejected_units": 1},
        ]
        result = self.store.load_initial(lots, samples)
        self.assertEqual((result["lots_loaded"], result["samples_loaded"]), (3, 5))
        rows = self.store.summary()
        diffs = diff_summary(reference_summary(self.store.conn), rows)
        self.assertEqual(diffs, [])
        # 甲/S1：L1 有 3 样本，L2 无样本 → joined=3+1=4，样本 3，批次 2
        g = [r for r in rows if r["supplier"] == "甲"][0]
        self.assertEqual((g["joined_rows"], g["n_samples"], g["n_lots"],
                          g["n_grades"], g["rejected_sum"]), (4, 3, 2, 1, 3))
        # 乙/S2：空字符串 grade 算一个不同非 null 值
        g2 = [r for r in rows if r["supplier"] == "乙"][0]
        self.assertEqual((g2["n_grades"], g2["rejected_sum"]), (1, 7))
        # 孤立样本 S5 不进任何组
        self.assertEqual(sum(r["n_samples"] for r in rows), 4)

    def test_identical_rows_with_distinct_sample_ids_count_twice(self):
        row = {"sample_id": "S1", "lot_id": "L1", "grade": "g", "rejected_units": 2}
        row2 = dict(row, sample_id="S2")  # 其他值完全相同，仍是两条样本
        self.store.load_initial(
            [{"lot_id": "L1", "supplier": "甲", "site": "S1"}], [row, row2])
        (g,) = self.store.summary()
        self.assertEqual((g["joined_rows"], g["n_samples"], g["n_lots"],
                          g["n_grades"], g["rejected_sum"]), (2, 2, 1, 1, 4))

    def test_load_requires_empty_db(self):
        self.store.load_initial([{"lot_id": "L1", "supplier": "甲", "site": "S1"}], [])
        with self.assertRaises(QcsumState):
            self.store.load_initial([], [])

    def test_load_rejects_duplicate_pk_and_bad_rows_atomically(self):
        lots = [{"lot_id": "L1", "supplier": "甲", "site": "S1"},
                {"lot_id": "L1", "supplier": "乙", "site": "S2"}]
        with self.assertRaises(QcsumValidation):
            self.store.load_initial(lots, [])
        self.assertEqual(self.store.base_rows("lots"), [])  # 整体回滚
        with self.assertRaises(QcsumValidation):
            self.store.load_initial([], [{"sample_id": "S1", "lot_id": "L1",
                                          "grade": "g"}])  # 缺列
        self.assertEqual(self.store.base_rows("samples"), [])

    def test_generated_medium_load_matches_reference(self):
        lots = list(generate_lots(200, seed=7))
        samples = list(generate_samples(5000, 200, seed=7))
        self.store.load_initial(iter(lots), iter(samples))
        diffs = diff_summary(reference_summary(self.store.conn), self.store.summary())
        self.assertEqual(diffs, [])


if __name__ == "__main__":
    unittest.main()
