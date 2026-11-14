"""八组场景逐步核对：每批应用后与完整 LEFT JOIN/GROUP BY 参考查询对比。"""
from __future__ import annotations

import tempfile
import unittest

from mes_pipeline.qcsum import SummaryStore
from mes_pipeline.qcsum.reference import diff_summary, reference_summary
from mes_pipeline.qcsum.scenarios import scenarios


def _group(rows, supplier, site):
    for r in rows:
        if r["supplier"] == supplier and r["site"] == site:
            return r
    return None


class ScenarioStepsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, sc_id):
        """逐步应用一组场景，每步与参考查询核对；返回每步后的汇总快照。"""
        (scenario,) = [s for s in scenarios() if s[0] == sc_id]
        _, _, steps = scenario
        store = SummaryStore(f"{self._tmp.name}/{sc_id}.db")
        snapshots = []
        try:
            for step_title, batch in steps:
                store.apply_batch(batch)
                rows = store.summary()
                diffs = diff_summary(reference_summary(store.conn), rows)
                self.assertEqual(diffs, [], f"[{sc_id}] {step_title}: {diffs}")
                snapshots.append(rows)
        finally:
            store.close()
        return snapshots

    def test_s1_lot_without_samples_then_first_and_second_sample(self):
        s1, s2, s3 = [ _group(s, "甲", "S1") for s in self._run("S1") ]
        self.assertEqual((s1["joined_rows"], s1["n_samples"], s1["n_lots"],
                          s1["n_grades"], s1["rejected_sum"]), (1, 0, 1, 0, None))
        self.assertEqual((s2["joined_rows"], s2["n_samples"], s2["n_grades"],
                          s2["rejected_sum"]), (1, 1, 1, 3))
        self.assertEqual((s3["joined_rows"], s3["n_samples"], s3["n_grades"],
                          s3["rejected_sum"]), (2, 2, 2, 3))

    def test_s2_same_grade_samples_removed_one_by_one(self):
        snaps = self._run("S2")
        g = [_group(s, "乙", "S1") for s in snaps]
        self.assertEqual(g[0]["n_grades"], 2)                    # x, y
        self.assertEqual(g[1]["n_grades"], 2)                    # x 还剩一条
        self.assertEqual((g[2]["n_grades"], g[2]["n_samples"]), (1, 1))
        self.assertEqual((g[3]["n_grades"], g[3]["n_samples"],
                          g[3]["joined_rows"], g[3]["rejected_sum"]),
                         (0, 0, 1, None))                        # 占位行回归

    def test_s3_null_vs_explicit_zero(self):
        snaps = self._run("S3")
        g = [_group(s, "丙", "S2") for s in snaps]
        self.assertIsNone(g[0]["rejected_sum"])                  # 全 null → null
        self.assertEqual(g[1]["rejected_sum"], 0)                # 有明确 0 → 0
        self.assertIsNone(g[2]["rejected_sum"])
        self.assertEqual(g[3]["rejected_sum"], 5)                # 5 + 0

    def test_s4_sample_moves_between_lots(self):
        snaps = self._run("S4")
        ga = _group(snaps[1], "甲", "S1")
        gb = _group(snaps[1], "乙", "S1")
        self.assertEqual((ga["n_samples"], ga["n_grades"], ga["rejected_sum"],
                          ga["joined_rows"]), (0, 0, None, 1))
        self.assertEqual((gb["n_samples"], gb["n_grades"], gb["rejected_sum"],
                          gb["joined_rows"]), (1, 1, 5, 1))

    def test_s5_lot_changes_group_and_samples_change_same_batch(self):
        snaps = self._run("S5")
        self.assertIsNone(_group(snaps[1], "甲", "S1"))          # 甲组整组消失
        gb = _group(snaps[1], "乙", "S1")
        # 乙组只计最终样本 E3(r,7)：E1/E2 既不漏减也不重复扣减
        self.assertEqual((gb["joined_rows"], gb["n_samples"], gb["n_lots"],
                          gb["n_grades"], gb["rejected_sum"]), (1, 1, 1, 1, 7))

    def test_s6_orphan_sample_lot_deleted_and_recreated(self):
        snaps = self._run("S6")
        self.assertEqual(snaps[0], [])                           # 孤立样本不进组
        g2 = _group(snaps[1], "丁", "S3")
        self.assertEqual((g2["n_samples"], g2["rejected_sum"]), (1, 4))
        self.assertEqual(snaps[2], [])                           # 批次删除 → 组消失
        g4 = _group(snaps[3], "丁", "S3")
        self.assertEqual((g4["n_samples"], g4["rejected_sum"]), (1, 4))  # 接回

    def test_s7_last_lot_leaves_group(self):
        snaps = self._run("S7")
        g = _group(snaps[1], "戊", "S4")
        self.assertEqual((g["n_lots"], g["joined_rows"], g["n_samples"]), (1, 1, 0))
        self.assertEqual(snaps[2], [])                           # 整组删除，不留全零空组

    def test_s8_delete_then_insert_same_key_is_net_zero(self):
        snaps = self._run("S8")
        self.assertEqual(snaps[1], snaps[0])                     # 样本删插净零
        self.assertEqual(snaps[2], snaps[0])                     # 批次删插净零

    def test_all_scenarios_end_to_end(self):
        for sc_id, _, _ in scenarios():
            with self.subTest(scenario=sc_id):
                self._run(sc_id)


if __name__ == "__main__":
    unittest.main()
