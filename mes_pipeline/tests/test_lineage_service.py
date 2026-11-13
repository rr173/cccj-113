"""服务层与 CLI 端到端测试：落盘、重读、失败结果、修正重装载、旧结果可查。"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from fractions import Fraction
from pathlib import Path

from mes_pipeline.lineage import service

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "lineage_demo"
S5 = DEMO / "s5_reject_then_fix"


class ServiceTests(unittest.TestCase):
    def test_load_save_show_recall_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            payload = service.load_lineage(
                DEMO / "s3_diamond" / "materials.json",
                DEMO / "s3_diamond" / "operations_reverse.json",
                d, calc_id="dia")
            self.assertEqual(payload["status"], "OK")
            self.assertTrue(payload["arithmetic"]["all_decimals_exact"])
            saved = service.load_saved(d, "dia")
            self.assertEqual(saved["calc_id"], "dia")

            comp = service.show_composition(d, "dia", "F-1")
            gen = {c["raw_batch_id"]: c["mass_g"]
                   for c in comp["generated_composition"]}
            self.assertEqual(gen, {"RM-X": "4375", "RM-Y": "2625"})

            recall = service.recall(d, "dia", ["RM-X"])
            self.assertEqual(recall["total_quarantine_mass_g"], "9000")
            for entry in recall["suspect_ledgers"]:
                self.assertTrue(entry["balanced"])

    def test_invalid_input_saved_and_no_genealogy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            payload = service.load_lineage(
                S5 / "materials.json",
                S5 / "operations_overdraw_1g.json",
                d, calc_id="bad")
            self.assertEqual(payload["status"], service.STATUS_INVALID)
            self.assertNotIn("batches", payload)
            loaded = service.load_saved(d, "bad")
            self.assertEqual(loaded["status"], "INVALID_INPUT")
            with self.assertRaises(ValueError):
                service.get_result(d, "bad")

    def test_fixed_reload_keeps_prior_results_queryable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            bad = service.load_lineage(
                S5 / "materials.json",
                S5 / "operations_missing_ref.json",
                d, calc_id="run-bad")
            self.assertEqual(bad["status"], "INVALID_INPUT")
            good = service.load_lineage(
                S5 / "materials.json",
                S5 / "operations_fixed_reverse.json",
                d, calc_id="run-good")
            self.assertEqual(good["status"], "OK")
            # 之前的失败结果与新结果都还在
            self.assertEqual(service.load_saved(d, "run-bad")["status"],
                             "INVALID_INPUT")
            self.assertEqual(service.load_saved(d, "run-good")["status"], "OK")
            comp = service.show_composition(d, "run-good", "H-2")
            self.assertEqual(comp["remaining_mass_g"], "4000")

    def test_missing_calc_id_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(KeyError):
                service.load_saved(d, "nope")

    def test_non_terminating_ratio_flagged_but_exact_fraction_kept(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            # 1kg + 2kg 混合后，可疑原料 R1 占比恰为 1/3（无限循环小数）
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
            service.load_lineage(
                tmp / "materials.json", tmp / "operations.json",
                d, calc_id="nt")
            recall = service.recall(d, "nt", ["R1"])
            # 占比 1/3 无法精确十进制展示 -> 全局标志置 False
            self.assertFalse(recall["all_decimals_exact"])
            item = next(it for it in recall["items"] if it["batch_id"] == "M")
            self.assertEqual(item["suspect_share_exact"], "1/3")
            # 100/3 百分数按 28 位小数 HALF_UP
            self.assertEqual(item["suspect_share_pct"],
                             "33." + "3" * 28)
            # 质量本身仍为有限小数，全局账精确平衡
            self.assertEqual(item["suspect_mass_g"], "1000")
            self.assertTrue(all(e["balanced"] for e in recall["suspect_ledgers"]))


class CliTests(unittest.TestCase):
    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        # -I 隔离模式下 PYTHONPATH 与 cwd 均不进 sys.path；项目以 editable
        # 安装时可直接 -m 调用。未安装环境（如 CI 精简镜像）通过 runpy 前置
        # 注入仓库根目录，两种环境下行为一致。
        bootstrap = (
            "import runpy, sys; "
            f"sys.path.insert(0, {str(ROOT)!r}); "
            "sys.argv = ['mes_pipeline.cli'] + "
            f"{list(args)!r}; "
            "runpy.run_module('mes_pipeline.cli', run_name='__main__')"
        )
        return subprocess.run(
            [sys.executable, "-I", "-c", bootstrap],
            cwd=ROOT, capture_output=True, text=True)

    def test_cli_full_flow(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            load = self._cli(
                "--store", d, "lineage-load",
                "--materials", str(DEMO / "s3_diamond" / "materials.json"),
                "--operations",
                str(DEMO / "s3_diamond" / "operations_reverse.json"),
                "--calc-id", "cli-dia")
            self.assertEqual(load.returncode, 0, load.stderr)
            self.assertEqual(json.loads(load.stdout)["status"], "OK")

            comp = self._cli("--store", d, "lineage-composition",
                             "--calc-id", "cli-dia", "--batch", "F-1")
            self.assertEqual(comp.returncode, 0, comp.stderr)

            recall = self._cli("--store", d, "lineage-recall",
                               "--calc-id", "cli-dia",
                               "--suspect", "RM-X", "--suspect", "RM-Y")
            self.assertEqual(recall.returncode, 0, recall.stderr)
            body = json.loads(recall.stdout)
            self.assertEqual(body["total_quarantine_mass_g"], "9000")

    def test_cli_rejection_exit_code_and_fix(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            bad = self._cli(
                "--store", d, "lineage-load",
                "--materials", str(S5 / "materials.json"),
                "--operations", str(S5 / "operations_cycle.json"),
                "--calc-id", "cli-bad")
            self.assertEqual(bad.returncode, 4)
            self.assertEqual(json.loads(bad.stdout)["status"], "INVALID_INPUT")

            fixed = self._cli(
                "--store", d, "lineage-load",
                "--materials", str(S5 / "materials.json"),
                "--operations", str(S5 / "operations_fixed_reverse.json"),
                "--calc-id", "cli-fixed")
            self.assertEqual(fixed.returncode, 0)

            missing = self._cli("--store", d, "lineage-composition",
                                "--calc-id", "ghost", "--batch", "X")
            self.assertEqual(missing.returncode, 2)

    def test_cli_unknown_batch_exit_code(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._cli(
                "--store", d, "lineage-load",
                "--materials", str(DEMO / "s1_mix_partial" / "materials.json"),
                "--operations",
                str(DEMO / "s1_mix_partial" / "operations.json"),
                "--calc-id", "s1")
            proc = self._cli("--store", d, "lineage-composition",
                             "--calc-id", "s1", "--batch", "GHOST")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("未知批次", proc.stderr)


if __name__ == "__main__":
    unittest.main()
