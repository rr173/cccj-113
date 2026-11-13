"""谱系引擎测试：精确有理数传播、菱形汇合、损耗分配、召回与全局账。

所有期望值均为手工独立核算的 Fraction，不引用生产代码中的中间量。
"""
from __future__ import annotations

import unittest
from fractions import Fraction as F
from pathlib import Path

from mes_pipeline.lineage.engine import (
    build_lineage,
    query_composition,
    run_recall,
)
from mes_pipeline.lineage.io import load_lineage_input

DEMO = Path(__file__).resolve().parent.parent / "lineage_demo"


def _load(scenario: str, ops_name: str = "operations.json"):
    raw, ops = load_lineage_input(
        DEMO / scenario / "materials.json",
        DEMO / scenario / ops_name)
    return build_lineage(raw, ops)


def _by_id(result):
    return {b.batch_id: b for b in result.batches}


def _comp(record, raw_id: str):
    return next(c for c in record.composition if c.raw_batch_id == raw_id).mass_g


def _gen(record, raw_id: str):
    return next(c for c in record.generated_composition
                if c.raw_batch_id == raw_id).mass_g


class MixPartialTests(unittest.TestCase):
    def setUp(self):
        self.result = _load("s1_mix_partial")
        self.batches = _by_id(self.result)

    def test_partial_consumption_keeps_remaining_composition(self):
        # RM-A 10kg 用 6kg，余 4kg，组成仍是纯 RM-A
        a = self.batches["RM-A"]
        self.assertEqual(a.remaining_mass_g, F(4000))
        self.assertEqual(_comp(a, "RM-A"), F(4000))
        b = self.batches["RM-B"]
        self.assertEqual(b.remaining_mass_g, F(3000))
        self.assertEqual(_comp(b, "RM-B"), F(3000))

    def test_mix_composition_proportional(self):
        # 8kg 混合批 = 6kg A + 2kg B，均匀混合占比 3:1
        m1 = self.batches["M-1"]
        self.assertEqual(m1.produced_mass_g, F(8000))
        self.assertEqual(_gen(m1, "RM-A"), F(6000))
        self.assertEqual(_gen(m1, "RM-B"), F(2000))

    def test_ledger_balanced(self):
        for entry in self.result.ledger:
            self.assertTrue(entry.balanced)
            self.assertEqual(
                entry.initial_g,
                entry.remaining_contribution_g + entry.loss_contribution_g)

    def test_recall_lists_only_positive_balance_with_suspect(self):
        recall = run_recall(self.result, ["RM-A"])
        ids = {it.batch_id for it in recall.items}
        self.assertEqual(ids, {"RM-A", "M-1"})  # RM-B 不含 A，不列入
        quarantine = {it.batch_id: it for it in recall.items}
        self.assertEqual(quarantine["M-1"].suspect_mass_g, F(6000))
        self.assertEqual(quarantine["M-1"].suspect_share, F(3, 4))
        # 整批隔离量 = 批的可用质量，不是只隔离可疑部分
        self.assertEqual(quarantine["M-1"].remaining_mass_g, F(8000))
        self.assertEqual(recall.total_quarantine_g, F(4000) + F(8000))


class SplitLossTests(unittest.TestCase):
    def setUp(self):
        self.result = _load("s2_split_loss")
        self.batches = _by_id(self.result)

    def test_one_input_many_outputs_with_loss(self):
        p1, p2 = self.batches["P-1"], self.batches["P-2"]
        self.assertEqual(p1.produced_mass_g, F(3000))
        self.assertEqual(p2.produced_mass_g, F(2500))
        # 损耗 4.5kg 只减量不选原料：全部来自 RM-C
        loss = self.result.losses[0]
        self.assertEqual(loss.total_loss_g, F(4500))
        self.assertEqual(loss.composition[0].raw_batch_id, "RM-C")
        self.assertEqual(loss.composition[0].mass_g, F(4500))

    def test_global_ledger_initial_equals_remaining_plus_loss(self):
        entry = self.result.ledger[0]
        self.assertEqual(entry.raw_batch_id, "RM-C")
        self.assertEqual(entry.initial_g, F(10_000))
        self.assertEqual(entry.remaining_contribution_g,
                         F(3000) + F(2500) + F(0))  # 原料批已耗尽
        self.assertEqual(entry.loss_contribution_g, F(4500))
        self.assertTrue(entry.balanced)


class DiamondTests(unittest.TestCase):
    def setUp(self):
        # 操作文件按倒序提供（OP-MERGE 在第一条）
        self.result = _load("s3_diamond", "operations_reverse.json")
        self.batches = _by_id(self.result)

    def test_topology_independent_of_record_order(self):
        m1 = self.batches["M-1"]
        self.assertEqual(_gen(m1, "RM-X"), F(10_000))
        self.assertEqual(_gen(m1, "RM-Y"), F(6_000))
        # 16kg 中两次共领走 14kg，余 2kg（X 1250g + Y 750g）
        self.assertEqual(m1.remaining_mass_g, F(2000))
        self.assertEqual(_comp(m1, "RM-X"), F(1250))
        self.assertEqual(_comp(m1, "RM-Y"), F(750))

    def test_branch_split_proportional_and_loss_uses_same_mix(self):
        d1 = self.batches["D-1"]  # 4kg 来自 8kg 领用，损耗 4kg
        self.assertTrue(d1.is_exhausted)
        self.assertEqual(_gen(d1, "RM-X"), F(2500))
        self.assertEqual(_gen(d1, "RM-Y"), F(1500))
        loss1 = next(l for l in self.result.losses
                     if l.operation_id == "OP-BRANCH1")
        self.assertEqual(_c(loss1, "RM-X"), F(2500))
        self.assertEqual(_c(loss1, "RM-Y"), F(1500))
        d2 = self.batches["D-2"]
        self.assertEqual(_gen(d2, "RM-X"), F(1875))
        self.assertEqual(_gen(d2, "RM-Y"), F(1125))

    def test_diamond_merge_adds_branches_without_dedup_or_double_count(self):
        f1 = self.batches["F-1"]
        x = next(c for c in f1.generated_composition
                 if c.raw_batch_id == "RM-X")
        y = next(c for c in f1.generated_composition
                 if c.raw_batch_id == "RM-Y")
        # 两支贡献相加：2500 + 1875 = 4375；1500 + 1125 = 2625
        self.assertEqual(x.mass_g, F(4375))
        self.assertEqual(y.mass_g, F(2625))
        self.assertEqual(len(x.paths), 2)  # 两支路径都保留，未按原料去重
        path_ops = {tuple(p.operation_chain) for p in x.paths}
        self.assertIn(("OP-MIX", "OP-BRANCH1", "OP-MERGE"), path_ops)
        self.assertIn(("OP-MIX", "OP-BRANCH2", "OP-MERGE"), path_ops)
        # 路径质量之和等于合计（不重复算祖先）
        self.assertEqual(sum((p.mass_g for p in x.paths), F(0)), F(4375))
        self.assertEqual(f1.produced_mass_g, F(7000))
        self.assertEqual(x.mass_g + y.mass_g, F(7000))

    def test_exhausted_intermediate_not_quarantined(self):
        recall = run_recall(self.result, ["RM-X", "RM-Y"])
        ids = {it.batch_id for it in recall.items}
        self.assertNotIn("D-1", ids)
        self.assertNotIn("D-2", ids)
        self.assertIn("F-1", ids)
        self.assertIn("M-1", ids)
        # 隔离总量只算在库批一次：F-1 7000 + M-1 余 2000
        self.assertEqual(recall.total_quarantine_g, F(9000))

    def test_single_suspect_ledger_and_quarantine(self):
        recall = run_recall(self.result, ["RM-X"])
        ledger = {e.raw_batch_id: e for e in recall.ledgers}
        self.assertEqual(set(ledger), {"RM-X"})
        entry = ledger["RM-X"]
        # 初始 10000 = 剩余贡献 (M-1 1250 + F-1 4375) + 损耗 (2500+1875)
        self.assertEqual(entry.remaining_contribution_g, F(5625))
        self.assertEqual(entry.loss_contribution_g, F(4375))
        self.assertTrue(entry.balanced)
        # 只隔离含 X 的批；M-1 与 F-1 都含
        self.assertEqual({it.batch_id for it in recall.items}, {"M-1", "F-1"})

    def test_batch_with_two_suspects_quarantined_once(self):
        recall = run_recall(self.result, ["RM-X", "RM-Y"])
        f1 = next(it for it in recall.items if it.batch_id == "F-1")
        self.assertEqual(f1.suspect_raw_batches, ("RM-X", "RM-Y"))
        self.assertEqual(f1.suspect_mass_g, F(7000))
        self.assertEqual(f1.remaining_mass_g, F(7000))  # 整批只计一次
        self.assertEqual(f1.suspect_share, F(1))

    def test_unknown_suspect_rejected(self):
        with self.assertRaises(KeyError):
            run_recall(self.result, ["NOPE"])


def _c(loss_record, raw_id: str):
    return next(c for c in loss_record.composition
                if c.raw_batch_id == raw_id).mass_g


class ExactExhaustTests(unittest.TestCase):
    def setUp(self):
        self.result = _load("s4_exact_exhaust")
        self.batches = _by_id(self.result)

    def test_mixed_g_kg_inputs_and_exact_exhaustion(self):
        p = self.batches["RM-P"]
        q = self.batches["RM-Q"]
        self.assertTrue(p.is_exhausted and q.is_exhausted)
        # E-1 3kg / E-2 2kg 按 4:1 混合组成（P 0.8, Q 0.2）
        e1 = self.batches["E-1"]
        self.assertEqual(_gen(e1, "RM-P"), F(2400))
        self.assertEqual(_gen(e1, "RM-Q"), F(600))
        e2 = self.batches["E-2"]
        self.assertEqual(_gen(e2, "RM-P"), F(1600))
        self.assertEqual(_gen(e2, "RM-Q"), F(400))
        # 全局守恒
        for entry in self.result.ledger:
            self.assertTrue(entry.balanced)
            self.assertEqual(entry.loss_contribution_g, F(0))

    def test_composition_query_unknown_batch(self):
        with self.assertRaises(KeyError):
            query_composition(self.result, "UNKNOWN")


class NonTerminatingArithmeticTests(unittest.TestCase):
    def test_fraction_paths_stay_exact_under_repeating_decimal(self):
        # 10kg 原料分三份各 1/3：比例 1/3 是无限循环小数，内部仍精确
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "materials.json").write_text(json.dumps({"batches": [
                {"batch_id": "R1", "material": "m",
                 "initial_mass": {"mass": "9", "unit": "kg"}},
            ]}), encoding="utf-8")
            (tmp / "operations.json").write_text(json.dumps({"operations": [
                {"operation_id": "OP1",
                 "inputs": [{"batch_id": "R1", "mass": "3", "unit": "kg"}],
                 "outputs": [
                     {"batch_id": "A", "mass": "1", "unit": "kg"},
                     {"batch_id": "B", "mass": "1", "unit": "kg"},
                     {"batch_id": "C", "mass": "1", "unit": "kg"}],
                 "loss_mass": {"mass": "0", "unit": "g"}},
            ]}), encoding="utf-8")
            raw, ops = load_lineage_input(
                tmp / "materials.json", tmp / "operations.json")
            result = build_lineage(raw, ops)
            a = _by_id(result)["A"]
            # 每份恰好 1kg R1
            self.assertEqual(_gen(a, "R1"), F(1000))
            # 原料剩余 6kg 仍精确
            self.assertEqual(_by_id(result)["R1"].remaining_mass_g, F(6000))

    def test_non_terminating_share_preserved_as_fraction(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "materials.json").write_text(json.dumps({"batches": [
                {"batch_id": "R1", "material": "m",
                 "initial_mass": {"mass": "1", "unit": "kg"}},
                {"batch_id": "R2", "material": "n",
                 "initial_mass": {"mass": "2", "unit": "kg"}},
            ]}), encoding="utf-8")
            (tmp / "operations.json").write_text(json.dumps({"operations": [
                {"operation_id": "OP1",
                 "inputs": [
                     {"batch_id": "R1", "mass": "1", "unit": "kg"},
                     {"batch_id": "R2", "mass": "2", "unit": "kg"}],
                 "outputs": [{"batch_id": "M", "mass": "3", "unit": "kg"}],
                 "loss_mass": {"mass": "0", "unit": "g"}},
            ]}), encoding="utf-8")
            raw, ops = load_lineage_input(
                tmp / "materials.json", tmp / "operations.json")
            result = build_lineage(raw, ops)
            recall = run_recall(result, ["R1"])
            item = next(it for it in recall.items if it.batch_id == "M")
            self.assertEqual(item.suspect_share, F(1, 3))  # 精确 1/3


if __name__ == "__main__":
    unittest.main()
