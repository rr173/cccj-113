"""确定性规模材料生成器（十万批次、百万样本量级）与基准批次构造。"""
from __future__ import annotations

import random

SUPPLIERS = [f"SUP-{i:03d}" for i in range(50)]
SITES = [f"SITE-{i:02d}" for i in range(12)]
#: 含空字符串（有实际含义的 grade）与中文等级
GRADES = ([f"G{i:02d}" for i in range(14)] + ["", "优等", "次品"])


def generate_lots(n_lots, seed=42):
    """确定性生成 n_lots 个批次行（dict 迭代器）。"""
    rng = random.Random(seed)
    for i in range(n_lots):
        yield {
            "lot_id": f"L{i:07d}",
            "supplier": rng.choice(SUPPLIERS),
            "site": rng.choice(SITES),
        }


def generate_samples(n_samples, n_lots, seed=42, orphan_rate=0.01):
    """确定性生成 n_samples 条样本行；约 orphan_rate 比例引用不存在的批次。"""
    rng = random.Random(seed + 1)
    for i in range(n_samples):
        if rng.random() < orphan_rate:
            lot_id = f"L{rng.randrange(n_lots, n_lots + 1000):07d}"  # 孤立样本
        else:
            lot_id = f"L{rng.randrange(n_lots):07d}"
        r = rng.random()
        grade = None if r < 0.25 else rng.choice(GRADES)
        r2 = rng.random()
        if r2 < 0.30:
            rejected = None
        elif r2 < 0.35:
            rejected = 0  # 明确的 0
        else:
            rejected = rng.randint(1, 1_000_000)
        yield {
            "sample_id": f"S{i:08d}",
            "lot_id": lot_id,
            "grade": grade,
            "rejected_units": rejected,
        }


def pick_small_lot(conn):
    """挑一个只有少量样本（1..5 条）的现存批次，返回 (lot_id, n_samples)。"""
    row = conn.execute(
        "SELECT a.lot_id, a.n_samples FROM qc_sample_agg a"
        " JOIN lots l ON l.lot_id = a.lot_id"
        " WHERE a.n_samples BETWEEN 1 AND 5"
        " ORDER BY a.n_samples, a.lot_id LIMIT 1").fetchone()
    if row is None:  # 兜底：样本最少的现存批次
        row = conn.execute(
            "SELECT a.lot_id, a.n_samples FROM qc_sample_agg a"
            " JOIN lots l ON l.lot_id = a.lot_id"
            " ORDER BY a.n_samples, a.lot_id LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("没有任何带样本的批次可挑")
    return row[0], row[1]


def build_batch_move_small_lot(conn, batch_id):
    """基准批次 A：小样本批换供应商（换组），同批删一条旧样本、加一条新样本。"""
    lot_id, _ = pick_small_lot(conn)
    site = conn.execute("SELECT site FROM lots WHERE lot_id=?",
                        (lot_id,)).fetchone()[0]
    samples = conn.execute(
        "SELECT sample_id FROM samples WHERE lot_id=? ORDER BY sample_id",
        (lot_id,)).fetchall()
    ops = [
        {"op": "update", "table": "lots", "key": lot_id,
         "row": {"lot_id": lot_id, "supplier": "SUP-BENCH-NEW", "site": site}},
        {"op": "delete", "table": "samples", "key": samples[0][0]},
        {"op": "insert", "table": "samples", "key": "S-BENCH-A1",
         "row": {"sample_id": "S-BENCH-A1", "lot_id": lot_id,
                 "grade": "bench", "rejected_units": 0}},
    ]
    return {"batch_id": batch_id, "ops": ops}


def build_batch_cross_transfer(conn, batch_id):
    """基准批次 B：把源批的 3 条样本跨组转移到另一组的目标批。"""
    src = conn.execute(
        "SELECT a.lot_id FROM qc_sample_agg a JOIN lots l ON l.lot_id = a.lot_id"
        " WHERE a.n_samples >= 3 ORDER BY a.lot_id LIMIT 1").fetchone()[0]
    src_group = conn.execute(
        "SELECT supplier, site FROM lots WHERE lot_id=?", (src,)).fetchone()
    dst = conn.execute(
        "SELECT lot_id FROM lots WHERE NOT (supplier=? AND site=?)"
        " ORDER BY lot_id LIMIT 1",
        (src_group[0], src_group[1])).fetchone()[0]
    moved = conn.execute(
        "SELECT sample_id, grade, rejected_units FROM samples"
        " WHERE lot_id=? ORDER BY sample_id LIMIT 3", (src,)).fetchall()
    ops = [
        {"op": "update", "table": "samples", "key": m[0],
         "row": {"sample_id": m[0], "lot_id": dst, "grade": m[1],
                 "rejected_units": m[2]}}
        for m in moved
    ]
    return {"batch_id": batch_id, "ops": ops}
