"""八组演示场景的小材料：每组场景是一串有序批次，逐步展示汇总变化。

场景与步骤是演示、测试、核对三方共用的单一事实来源：
- `qcsum demo` 逐步应用并打印汇总；
- tests/test_qcsum_steps.py 逐步与参考查询核对；
- qcsum_demo/generate.py 把同样的批次落成 JSON 材料供 verify.py 重放。
"""
from __future__ import annotations


def _ins_lot(lot_id, supplier, site):
    return {"op": "insert", "table": "lots", "key": lot_id,
            "row": {"lot_id": lot_id, "supplier": supplier, "site": site}}


def _upd_lot(lot_id, supplier, site):
    return {"op": "update", "table": "lots", "key": lot_id,
            "row": {"lot_id": lot_id, "supplier": supplier, "site": site}}


def _ins_sample(sample_id, lot_id, grade, rejected):
    return {"op": "insert", "table": "samples", "key": sample_id,
            "row": {"sample_id": sample_id, "lot_id": lot_id,
                    "grade": grade, "rejected_units": rejected}}


def _upd_sample(sample_id, lot_id, grade, rejected):
    return {"op": "update", "table": "samples", "key": sample_id,
            "row": {"sample_id": sample_id, "lot_id": lot_id,
                    "grade": grade, "rejected_units": rejected}}


def _del(table, key):
    return {"op": "delete", "table": table, "key": key}


def _batch(batch_id, *ops):
    return {"batch_id": batch_id, "ops": list(ops)}


def scenarios():
    """返回 [(场景号, 场景标题, [(步骤标题, 批次), ...]), ...]；每组场景独立空库。"""
    return [
        ("S1", "无样本批次加入首条和第二条样本", [
            ("建批次 L1（甲/S1），无样本 → 一条左连接占位行",
             _batch("S1-B1", _ins_lot("L1", "甲", "S1"))),
            ("首条样本 A1（grade=g1, rejected=3）",
             _batch("S1-B2", _ins_sample("A1", "L1", "g1", 3))),
            ("第二条样本 A2（grade=g2, rejected=null）",
             _batch("S1-B3", _ins_sample("A2", "L1", "g2", None))),
        ]),
        ("S2", "同 grade 多样本逐条撤销", [
            ("建 L2（乙/S1）+ B1/B2 同 grade=x，B3 grade=y",
             _batch("S2-B1", _ins_lot("L2", "乙", "S1"),
                    _ins_sample("B1", "L2", "x", 1),
                    _ins_sample("B2", "L2", "x", 2),
                    _ins_sample("B3", "L2", "y", None))),
            ("撤销 B1：x 还剩一条，不同 grade 数不变",
             _batch("S2-B2", _del("samples", "B1"))),
            ("撤销 B2：x 最后一条离开，不同 grade 数减一",
             _batch("S2-B3", _del("samples", "B2"))),
            ("撤销 B3：批次回到无样本占位行",
             _batch("S2-B4", _del("samples", "B3"))),
        ]),
        ("S3", "null 数量与明确 0", [
            ("建 L3（丙/S2）+ C1 rejected=null → 和为 null",
             _batch("S3-B1", _ins_lot("L3", "丙", "S2"),
                    _ins_sample("C1", "L3", "g", None))),
            ("加入 C2 rejected=0 → 和为明确的 0",
             _batch("S3-B2", _ins_sample("C2", "L3", "g", 0))),
            ("撤销 C2 → 和回到 null",
             _batch("S3-B3", _del("samples", "C2"))),
            ("加入 C3=5、C4=0 → 和为 5",
             _batch("S3-B4", _ins_sample("C3", "L3", "h", 5),
                    _ins_sample("C4", "L3", "h", 0))),
        ]),
        ("S4", "样本跨批次转移", [
            ("建 L4a（甲/S1）、L4b（乙/S1）+ D1 挂在 L4a",
             _batch("S4-B1", _ins_lot("L4a", "甲", "S1"),
                    _ins_lot("L4b", "乙", "S1"),
                    _ins_sample("D1", "L4a", "g", 5))),
            ("D1 改挂 L4b：两组的样本数/和/grade 数同步迁移",
             _batch("S4-B2", _upd_sample("D1", "L4b", "g", 5))),
        ]),
        ("S5", "批次更换分组且同批改样本", [
            ("建 L5（甲/S1）+ E1(p,1)、E2(q,2)",
             _batch("S5-B1", _ins_lot("L5", "甲", "S1"),
                    _ins_sample("E1", "L5", "p", 1),
                    _ins_sample("E2", "L5", "q", 2))),
            ("同批：L5 改归（乙/S1），删 E1/E2，加 E3(r,7) → 乙只计最终样本",
             _batch("S5-B2", _upd_lot("L5", "乙", "S1"),
                    _del("samples", "E1"), _del("samples", "E2"),
                    _ins_sample("E3", "L5", "r", 7))),
        ]),
        ("S6", "孤立样本先到、批次删除及重建", [
            ("F1 引用尚未到达的 L6 → 不进任何组",
             _batch("S6-B1", _ins_sample("F1", "L6", "g", 4))),
            ("L6（丁/S3）到达 → F1 接回进组",
             _batch("S6-B2", _ins_lot("L6", "丁", "S3"))),
            ("删除 L6 → 组随之消失，F1 再次孤立",
             _batch("S6-B3", _del("lots", "L6"))),
            ("重建 L6 → F1 再次接回",
             _batch("S6-B4", _ins_lot("L6", "丁", "S3"))),
        ]),
        ("S7", "最后一个批次离组", [
            ("建 L7、L7b 同组（戊/S4）+ G1 挂 L7",
             _batch("S7-B1", _ins_lot("L7", "戊", "S4"),
                    _ins_lot("L7b", "戊", "S4"),
                    _ins_sample("G1", "L7", "g", 1))),
            ("删除 L7 → 组仍在（L7b 贡献占位行）",
             _batch("S7-B2", _del("lots", "L7"))),
            ("删除 L7b → 最后一个批次离组，整组删除",
             _batch("S7-B3", _del("lots", "L7b"))),
        ]),
        ("S8", "同键先删后插形成净零变化", [
            ("建 L8（己/S5）+ H1(g,9)",
             _batch("S8-B1", _ins_lot("L8", "己", "S5"),
                    _ins_sample("H1", "L8", "g", 9))),
            ("同批删 H1 再插回完全相同行 → 汇总不变",
             _batch("S8-B2", _del("samples", "H1"),
                    _ins_sample("H1", "L8", "g", 9))),
            ("同批删 L8 再插回完全相同行 → 样本接回，汇总不变",
             _batch("S8-B3", _del("lots", "L8"),
                    _ins_lot("L8", "己", "S5"))),
        ]),
    ]


def iter_steps():
    """展开为 (场景号, 场景标题, 步骤标题, 批次) 序列。"""
    for sc_id, sc_title, steps in scenarios():
        for step_title, batch in steps:
            yield sc_id, sc_title, step_title, batch
