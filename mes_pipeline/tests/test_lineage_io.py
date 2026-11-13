"""装载期校验测试：单据级问题、交叉引用、成环与累计超领全部定位拒绝。"""
from __future__ import annotations

import json
import unittest
from fractions import Fraction
from pathlib import Path

from mes_pipeline.lineage.io import LineageInputError, load_lineage_input

DEMO = Path(__file__).resolve().parent.parent / "lineage_demo"
S5 = DEMO / "s5_reject_then_fix"


class IoRejectionTests(unittest.TestCase):
    def _reject(self, materials: Path, operations: Path) -> list[str]:
        with self.assertRaises(LineageInputError) as ctx:
            load_lineage_input(materials, operations)
        return ctx.exception.problems

    def test_overdraw_by_one_gram_rejected_with_location(self):
        problems = self._reject(
            S5 / "materials.json", S5 / "operations_overdraw_1g.json")
        self.assertEqual(len(problems), 1)
        self.assertIn("超领 1 g", problems[0])
        self.assertIn("RM-Y", problems[0])
        self.assertIn("操作记录#1", problems[0])

    def test_mass_imbalance_rejected_exactly(self):
        problems = self._reject(
            S5 / "materials.json", S5 / "operations_unbalanced.json")
        self.assertTrue(any("质量不守恒" in p and "差额 1000 g" in p
                            for p in problems))

    def test_missing_reference_rejected(self):
        problems = self._reject(
            S5 / "materials.json", S5 / "operations_missing_ref.json")
        self.assertTrue(any("引用了不存在的批 RM-GHOST" in p
                            and "输入行#1" in p for p in problems))

    def test_cycle_rejected_with_operation_chain(self):
        problems = self._reject(
            S5 / "materials.json", S5 / "operations_cycle.json")
        self.assertTrue(any("循环依赖" in p and "OP-CYC-A" in p
                            and "OP-CYC-B" in p for p in problems))

    def test_fixed_reverse_order_loads(self):
        raw, ops = load_lineage_input(
            S5 / "materials.json", S5 / "operations_fixed_reverse.json")
        self.assertEqual([op.operation_id for op in ops][:1], ["OP-STEP2"])
        self.assertEqual(len(raw), 2)

    def test_reverse_order_is_not_rejected(self):
        # 倒序记录：先出现的操作消费后出现操作的产出，必须正常装载
        raw, ops = load_lineage_input(
            DEMO / "s3_diamond" / "materials.json",
            DEMO / "s3_diamond" / "operations_reverse.json")
        self.assertEqual(len(ops), 4)

    def _write(self, tmp: Path, payload: dict) -> Path:
        path = tmp / "operations.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_duplicate_batch_within_operation_rejected(self):
        import tempfile
        materials = DEMO / "s1_mix_partial" / "materials.json"
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            payload = {"operations": [{
                "operation_id": "OP-DUP",
                "inputs": [
                    {"batch_id": "RM-A", "mass": "1", "unit": "kg"},
                    {"batch_id": "RM-A", "mass": "2", "unit": "kg"},
                ],
                "outputs": [{"batch_id": "Z-1", "mass": "3", "unit": "kg"}],
                "loss_mass": {"mass": "0", "unit": "g"},
            }]}
            problems = self._reject(materials, self._write(tmp, payload))
            self.assertTrue(any("重复列出" in p for p in problems))

    def test_duplicate_operation_id_rejected(self):
        import tempfile
        materials = DEMO / "s1_mix_partial" / "materials.json"
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            payload = {"operations": [
                {"operation_id": "OP",
                 "inputs": [{"batch_id": "RM-A", "mass": "1", "unit": "kg"}],
                 "outputs": [{"batch_id": "Z-1", "mass": "1", "unit": "kg"}],
                 "loss_mass": {"mass": "0", "unit": "g"}},
                {"operation_id": "OP",
                 "inputs": [{"batch_id": "RM-B", "mass": "1", "unit": "kg"}],
                 "outputs": [{"batch_id": "Z-2", "mass": "1", "unit": "kg"}],
                 "loss_mass": {"mass": "0", "unit": "g"}},
            ]}
            problems = self._reject(materials, self._write(tmp, payload))
            self.assertTrue(any("operation_id 重复" in p for p in problems))

    def test_output_overwrites_raw_batch_rejected(self):
        import tempfile
        materials = DEMO / "s1_mix_partial" / "materials.json"
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            payload = {"operations": [{
                "operation_id": "OP",
                "inputs": [{"batch_id": "RM-B", "mass": "1", "unit": "kg"}],
                "outputs": [{"batch_id": "RM-A", "mass": "1", "unit": "kg"}],
                "loss_mass": {"mass": "0", "unit": "g"},
            }]}
            problems = self._reject(materials, self._write(tmp, payload))
            self.assertTrue(any("不允许覆盖" in p for p in problems))

    def test_second_producer_rejected(self):
        import tempfile
        materials = DEMO / "s1_mix_partial" / "materials.json"
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            payload = {"operations": [
                {"operation_id": "OP1",
                 "inputs": [{"batch_id": "RM-A", "mass": "1", "unit": "kg"}],
                 "outputs": [{"batch_id": "Z-9", "mass": "1", "unit": "kg"}],
                 "loss_mass": {"mass": "0", "unit": "g"}},
                {"operation_id": "OP2",
                 "inputs": [{"batch_id": "RM-B", "mass": "1", "unit": "kg"}],
                 "outputs": [{"batch_id": "Z-9", "mass": "1", "unit": "kg"}],
                 "loss_mass": {"mass": "0", "unit": "g"}},
            ]}
            problems = self._reject(materials, self._write(tmp, payload))
            self.assertTrue(any("只能有一个生产者" in p for p in problems))

    def test_non_positive_and_bad_unit_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            mat = tmp / "materials.json"
            mat.write_text(json.dumps({"batches": [
                {"batch_id": "R1", "material": "m",
                 "initial_mass": {"mass": "0", "unit": "kg"}},
                {"batch_id": "R2", "material": "m",
                 "initial_mass": {"mass": "1", "unit": "ton"}},
            ]}), encoding="utf-8")
            ops = tmp / "operations.json"
            ops.write_text(json.dumps({"operations": []}), encoding="utf-8")
            problems = self._reject(mat, ops)
            self.assertTrue(any("必须为正" in p for p in problems))
            self.assertTrue(any("单位不明" in p for p in problems))

    def test_float_literal_rejected_no_tolerance_swallowing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            mat = tmp / "materials.json"
            mat.write_text(json.dumps({"batches": [
                {"batch_id": "R1", "material": "m",
                 "initial_mass": {"mass": 1.1, "unit": "kg"}},
            ]}), encoding="utf-8")
            ops = tmp / "operations.json"
            ops.write_text(json.dumps({"operations": []}), encoding="utf-8")
            problems = self._reject(mat, ops)
            self.assertTrue(any("不接受浮点字面量" in p for p in problems))

    def test_cumulative_overdraw_orders_independent(self):
        # 同一批分两次领用，合计超 1 g；调换两条操作顺序结论不变
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            mat = tmp / "materials.json"
            mat.write_text(json.dumps({"batches": [
                {"batch_id": "R1", "material": "m",
                 "initial_mass": {"mass": "1000", "unit": "g"}},
            ]}), encoding="utf-8")

            def make(first: str, second: str) -> Path:
                payload = {"operations": [
                    {"operation_id": first,
                     "inputs": [{"batch_id": "R1", "mass": "600", "unit": "g"}],
                     "outputs": [{"batch_id": first + "-O",
                                  "mass": "600", "unit": "g"}],
                     "loss_mass": {"mass": "0", "unit": "g"}},
                    {"operation_id": second,
                     "inputs": [{"batch_id": "R1", "mass": "401", "unit": "g"}],
                     "outputs": [{"batch_id": second + "-O",
                                  "mass": "401", "unit": "g"}],
                     "loss_mass": {"mass": "0", "unit": "g"}},
                ]}
                p = tmp / f"ops-{first}.json"
                p.write_text(json.dumps(payload), encoding="utf-8")
                return p

            p1 = make("A", "B")
            p2 = make("B", "A")
            for p in (p1, p2):
                problems = self._reject(mat, p)
                self.assertTrue(any("超领 1 g" in p for p in problems))

    def test_unit_conversion_is_exact(self):
        raw, _ = load_lineage_input(
            DEMO / "s1_mix_partial" / "materials.json",
            DEMO / "s1_mix_partial" / "operations.json")
        by_id = {b.batch_id: b for b in raw}
        self.assertEqual(by_id["RM-A"].initial.grams, Fraction(10_000))
        self.assertEqual(by_id["RM-B"].initial.grams, Fraction(5_000))

    def test_same_batch_consumed_by_multiple_operations_ok(self):
        # 同批分两次领用给不同操作，合计不超可用量即合法
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "materials.json").write_text(json.dumps({"batches": [
                {"batch_id": "R1", "material": "m",
                 "initial_mass": {"mass": "1000", "unit": "g"}},
            ]}), encoding="utf-8")
            (tmp / "operations.json").write_text(json.dumps({"operations": [
                {"operation_id": "A",
                 "inputs": [{"batch_id": "R1", "mass": "600", "unit": "g"}],
                 "outputs": [{"batch_id": "A-O", "mass": "600", "unit": "g"}],
                 "loss_mass": {"mass": "0", "unit": "g"}},
                {"operation_id": "B",
                 "inputs": [{"batch_id": "R1", "mass": "400", "unit": "g"}],
                 "outputs": [{"batch_id": "B-O", "mass": "400", "unit": "g"}],
                 "loss_mass": {"mass": "0", "unit": "g"}},
            ]}), encoding="utf-8")
            raw, ops = load_lineage_input(
                tmp / "materials.json", tmp / "operations.json")
            self.assertEqual(len(ops), 2)

    def test_nan_constant_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            mat = tmp / "materials.json"
            mat.write_text('{"batches": [{"batch_id": "R1", "material": "m",'
                           ' "initial_mass": {"mass": NaN, "unit": "g"}}]}',
                           encoding="utf-8")
            ops = tmp / "operations.json"
            ops.write_text('{"operations": []}', encoding="utf-8")
            problems = self._reject(mat, ops)
            self.assertTrue(any("JSON" in p for p in problems))

    def test_negative_loss_rejected_but_zero_allowed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "materials.json").write_text(json.dumps({"batches": [
                {"batch_id": "R1", "material": "m",
                 "initial_mass": {"mass": "1000", "unit": "g"}},
            ]}), encoding="utf-8")
            bad = tmp / "bad.json"
            bad.write_text(json.dumps({"operations": [
                {"operation_id": "OP",
                 "inputs": [{"batch_id": "R1", "mass": "500", "unit": "g"}],
                 "outputs": [{"batch_id": "O", "mass": "600", "unit": "g"}],
                 "loss_mass": {"mass": "-100", "unit": "g"}},
            ]}), encoding="utf-8")
            problems = self._reject(tmp / "materials.json", bad)
            self.assertTrue(any("loss_mass" in p and "非负" in p
                                for p in problems))
            ok = tmp / "ok.json"
            ok.write_text(json.dumps({"operations": [
                {"operation_id": "OP",
                 "inputs": [{"batch_id": "R1", "mass": "500", "unit": "g"}],
                 "outputs": [{"batch_id": "O", "mass": "500", "unit": "g"}],
                 "loss_mass": {"mass": "0", "unit": "g"}},
            ]}), encoding="utf-8")
            _, ops = load_lineage_input(tmp / "materials.json", ok)
            self.assertEqual(ops[0].loss.grams, Fraction(0))


if __name__ == "__main__":
    unittest.main()
