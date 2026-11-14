"""输入约定测试：缺列/多列/重复对象键/类型与取值/主键不可改/批次结构。"""
from __future__ import annotations

import tempfile
import unittest

from mes_pipeline.qcsum import (
    QcsumValidation,
    SummaryStore,
    loads_strict,
    validate_batch,
    validate_row,
)


def _lot(lot_id="L1", supplier="甲", site="S1"):
    return {"lot_id": lot_id, "supplier": supplier, "site": site}


def _sample(sample_id="S1", lot_id="L1", grade="g", rejected=0):
    return {"sample_id": sample_id, "lot_id": lot_id,
            "grade": grade, "rejected_units": rejected}


def _batch(*ops, batch_id="B1"):
    return {"batch_id": batch_id, "ops": list(ops)}


def _ins(table, row):
    key = row["lot_id"] if table == "lots" else row["sample_id"]
    return {"op": "insert", "table": table, "key": key, "row": row}


class LoadsStrictTest(unittest.TestCase):
    def test_duplicate_object_key_rejected(self):
        with self.assertRaises(QcsumValidation):
            loads_strict('{"batch_id": "B1", "batch_id": "B2", "ops": []}')

    def test_duplicate_row_key_rejected(self):
        text = ('{"batch_id": "B1", "ops": [{"op": "insert", "table": "lots",'
                ' "key": "L1", "row": {"lot_id": "L1", "lot_id": "L2",'
                ' "supplier": "甲", "site": "S1"}}]}')
        with self.assertRaises(QcsumValidation):
            loads_strict(text)

    def test_bad_json_rejected(self):
        with self.assertRaises(QcsumValidation):
            loads_strict("{not json")


class RowValidationTest(unittest.TestCase):
    def test_valid_rows_accepted(self):
        validate_row("lots", _lot())
        validate_row("samples", _sample(grade=None, rejected=None))
        validate_row("samples", _sample(grade="", rejected=0))  # 空串 grade 合法
        validate_row("samples", _sample(rejected=1_000_000))

    def test_missing_and_extra_columns(self):
        bad = _lot()
        del bad["site"]
        with self.assertRaises(QcsumValidation):
            validate_row("lots", bad)
        bad = _lot()
        bad["extra"] = 1
        with self.assertRaises(QcsumValidation):
            validate_row("lots", bad)

    def test_id_and_group_fields_nonempty_str(self):
        for field, value in (("lot_id", ""), ("lot_id", None), ("lot_id", 7),
                             ("supplier", ""), ("site", None)):
            bad = _lot()
            bad[field] = value
            with self.assertRaises(QcsumValidation, msg=f"{field}={value!r}"):
                validate_row("lots", bad)

    def test_grade_str_or_null(self):
        for value in (0, 1.5, True, ["g"]):
            with self.assertRaises(QcsumValidation, msg=f"grade={value!r}"):
                validate_row("samples", _sample(grade=value))

    def test_rejected_units_int_or_null_no_bool(self):
        for value in (True, False, 1.0, "3", -1, 1_000_001):
            with self.assertRaises(QcsumValidation, msg=f"rejected={value!r}"):
                validate_row("samples", _sample(rejected=value))


class BatchValidationTest(unittest.TestCase):
    def test_batch_structure(self):
        for bad in ({}, {"batch_id": "B1"}, {"ops": []},
                    {"batch_id": "", "ops": []},
                    {"batch_id": "B1", "ops": [], "extra": 1},
                    {"batch_id": "B1", "ops": {}}):
            with self.assertRaises(QcsumValidation, msg=str(bad)):
                validate_batch(bad)

    def test_op_structure(self):
        base = _ins("lots", _lot())
        for bad in ({"op": "insert", "table": "lots", "key": "L1"},          # 缺 row
                    dict(base, extra=1),                                       # 多字段
                    {"op": "delete", "table": "lots", "key": "L1",
                     "row": _lot()},                                           # delete 多 row
                    {"op": "upsert", "table": "lots", "key": "L1",
                     "row": _lot()},                                           # 未知 op
                    {"op": "insert", "table": "grades", "key": "L1",
                     "row": _lot()},                                           # 未知表
                    {"op": "insert", "table": "lots", "key": "",
                     "row": _lot()}):                                          # 空 key
            with self.assertRaises(QcsumValidation, msg=str(bad)):
                validate_batch(_batch(bad))

    def test_pk_immutable(self):
        op = {"op": "update", "table": "lots", "key": "L1",
              "row": _lot(lot_id="L2")}
        with self.assertRaises(QcsumValidation):
            validate_batch(_batch(op))

    def test_valid_batch_normalized(self):
        batch_id, ops = validate_batch(
            _batch(_ins("lots", _lot()), {"op": "delete", "table": "lots", "key": "L9"}))
        self.assertEqual(batch_id, "B1")
        self.assertEqual([o["op"] for o in ops], ["insert", "delete"])


class ExistenceTest(unittest.TestCase):
    """insert/update/delete 的存在性约束按操作列表依次检查。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SummaryStore(f"{self._tmp.name}/t.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_insert_requires_absent(self):
        self.store.apply_batch(_batch(_ins("lots", _lot()), batch_id="B1"))
        with self.assertRaises(QcsumValidation):
            self.store.apply_batch(_batch(_ins("lots", _lot()), batch_id="B2"))

    def test_update_and_delete_require_present(self):
        with self.assertRaises(QcsumValidation):
            self.store.apply_batch(_batch(
                {"op": "update", "table": "lots", "key": "L1", "row": _lot()},
                batch_id="B1"))
        with self.assertRaises(QcsumValidation):
            self.store.apply_batch(_batch(
                {"op": "delete", "table": "samples", "key": "S1"}, batch_id="B2"))

    def test_repeated_change_and_delete_insert_same_batch(self):
        # 同批反复改同一键 + 先删后插，全部合法
        self.store.apply_batch(_batch(
            _ins("lots", _lot()),
            {"op": "update", "table": "lots", "key": "L1", "row": _lot(supplier="乙")},
            {"op": "update", "table": "lots", "key": "L1", "row": _lot(supplier="丙")},
            {"op": "delete", "table": "lots", "key": "L1"},
            _ins("lots", _lot(supplier="丁")),
            batch_id="B1"))
        self.assertEqual(self.store.base_rows("lots"),
                         [_lot(supplier="丁")])


if __name__ == "__main__":
    unittest.main()
