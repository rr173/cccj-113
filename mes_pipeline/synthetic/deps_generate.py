"""供货属性材料的生成器（确定性，无随机数）。

生成两类材料，均写成「列清单 JSON + 逐行 JSONL」：

1. **演示小表**（``demo`` 场景，写入 ``deps_demo/<场景>/``）：刻意覆盖
   仓库同事关心的判定边界，每张表都小而可手工核对：

   - ``s1_composite_key``        两列合起来唯一、各单列都不唯一；含重复整行
                                （row_id 不同）与一条空左部常量依赖；
   - ``s2_null_policies``        某列含 null，equal / ignore 两种口径结论不同；
   - ``s3_evidence_gap``         ignore 下 X → A 成立，但加一列后把参与行筛得
                                太少，变成证据不足；
   - ``s4_violation_to_hold``    X → A 违反，加一列后成立；复核反例仍是原值；
   - ``s5_bad_rows``             资料错误（少列/多列/重复 row_id/值类型）；
   - ``s6_bad_rows_fixed``       同一份资料修好后的版本；
   - ``s7_typo_columns``         列清单里重复列名（另一类资料错误）。

2. **规模样本**（``bench`` 场景，写入 ``deps_bench/``）：不小于 12 列、
   20000 行，用于实测发现耗时与内存；生成规则与生产搜索无关。

运行：
    .venv/bin/python -I -m synthetic.deps_generate --root deps_demo --scenario demo
    .venv/bin/python -I -m synthetic.deps_generate --root deps_bench --scenario bench
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

DEMO_COLUMNS = ["supplier", "part", "name", "grade", "package", "origin"]


def _write_table(root: Path, table_id: str, columns: Sequence[str],
                 rows: Sequence[Tuple[str, Sequence[Optional[str]]]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "columns.json").write_text(json.dumps(
        {"table_id": table_id, "columns": list(columns)},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (root / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for row_id, values in rows:
            record = {"row_id": row_id,
                      "values": {c: v for c, v in zip(columns, values)}}
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# 场景 1：二列合起来唯一，各单列都不唯一
# --------------------------------------------------------------------------- #
def scenario_composite_key(root: Path) -> None:
    columns = DEMO_COLUMNS
    # (supplier, part) 唯一；supplier 单列不唯一（S1 出现两次），
    # part 单列不唯一（P-01 在 S1、S2 各出现一次）。
    # origin 恒为 CN（空左部常量依赖）；package ↔ grade 一一对应。
    rows = [
        ("R-001", ["S1", "P-01", "轴承座", "A", "BOX-1", "CN"]),
        ("R-002", ["S1", "P-02", "端盖", "A", "BOX-1", "CN"]),
        ("R-003", ["S2", "P-01", "轴承座", "B", "BOX-2", "CN"]),
        ("R-004", ["S2", "P-03", "轴套", "B", "BOX-2", "CN"]),
        ("R-005", ["S3", "P-04", "垫圈", "C", "BOX-3", "CN"]),
        ("R-006", ["S3", "P-05", "卡簧", "C", "BOX-3", "CN"]),
    ]
    _write_table(root, "s1_composite_key", columns, rows)


# --------------------------------------------------------------------------- #
# 场景 2：空值口径结论不同
# --------------------------------------------------------------------------- #
def scenario_null_policies(root: Path) -> None:
    columns = DEMO_COLUMNS
    # name 在 N-004 缺失（null），其余行齐全。
    # - equal ：null 作为一种共同值参与比较，supplier=S2 的 name 出现
    #           「端盖」与 null 两种取值 → supplier → name 违反；
    # - ignore：排除含 null 的行后，S2 只剩「端盖」→ supplier → name 成立，
    #           但参与行数从 6 降到 5（排除数的差别也写进结果）。
    # (supplier, part) 在两种口径下都不是键：N-005/N-006 的 package 不同，
    # 所以这里不靠键的差异，纯粹展示同一依赖在两种口径下的相反结论。
    rows = [
        ("N-001", ["S1", "P-01", "轴承座", "A", "BOX-1", "CN"]),
        ("N-002", ["S1", "P-02", "轴承座", "A", "BOX-1", "CN"]),
        ("N-003", ["S2", "P-03", "端盖", "B", "BOX-2", "CN"]),
        ("N-004", ["S2", "P-04", None, "B", "BOX-2", "CN"]),
        ("N-005", ["S3", "P-05", "垫圈", "C", "BOX-3", "CN"]),
        ("N-006", ["S3", "P-05", "垫圈", "C", "BOX-4", "CN"]),
    ]
    _write_table(root, "s2_null_policies", columns, rows)


# --------------------------------------------------------------------------- #
# 场景 3：ignore 下成立 → 加列后证据不足
# --------------------------------------------------------------------------- #
def scenario_evidence_gap(root: Path) -> None:
    columns = ["supplier", "part", "batch", "cert", "name"]
    # supplier → name：6 行齐全、成立。
    # (supplier, batch) → name：batch 只有 G-001 一行非 null，参与行数 1，
    #   min_rows=2 时「无冲突但证据不足」——这正是加列改变参与行集合的效果，
    #   而不是改掉了任何右部取值。
    # batch 单列 → name 同样证据不足（参与 1 行），因此 (supplier,batch)→name
    #   不是被更小的成立依赖解释掉的，会作为证据缺口列出。
    rows = [
        ("G-001", ["S1", "P-01", "L1", None, "轴承座"]),
        ("G-002", ["S1", "P-02", None, None, "轴承座"]),
        ("G-003", ["S2", "P-03", None, None, "端盖"]),
        ("G-004", ["S2", "P-04", None, None, "端盖"]),
        ("G-005", ["S3", "P-05", None, "C-9", "垫圈"]),
        ("G-006", ["S3", "P-06", None, None, "垫圈"]),
        ("G-007", ["S4", "P-07", None, "C-10", "卡簧"]),
    ]
    _write_table(root, "s3_evidence_gap", columns, rows)


# --------------------------------------------------------------------------- #
# 场景 4：X → A 违反，加一列后成立（原值不改）
# --------------------------------------------------------------------------- #
def scenario_violation_to_hold(root: Path) -> None:
    columns = ["supplier", "part", "name", "origin"]
    # supplier → name 违反：S1 同时供「轴承座」与「端盖」。
    # 加 part 后 (supplier, part) → name 成立：S1 的两种名称分属不同零件。
    # 反例里的原始取值（S1 含两种名称）一字未改，只是新增列把它们区分开。
    rows = [
        ("V-001", ["S1", "P-01", "轴承座", "CN"]),
        ("V-002", ["S1", "P-02", "端盖", "CN"]),
        ("V-003", ["S2", "P-01", "轴承座", "CN"]),
        ("V-004", ["S2", "P-03", "轴套", "CN"]),
        ("V-005", ["S3", "P-04", "垫圈", "CN"]),
    ]
    _write_table(root, "s4_violation_to_hold", columns, rows)


def scenario_duplicate_rows(root: Path) -> None:
    columns = DEMO_COLUMNS
    # D-002 与 D-004 在全部列上完全相同（只有 row_id 不同）。
    # row_id 是资料的唯一标识，不参与任何键/依赖；因此在这个 k 范围内
    # 找不到任何唯一键——这不是搜索失败，而正是「整行重复」的后果。
    rows = [
        ("D-001", ["S1", "P-01", "轴承座", "A", "BOX-1", "CN"]),
        ("D-002", ["S1", "P-02", "端盖", "A", "BOX-1", "CN"]),
        ("D-003", ["S2", "P-03", "轴套", "B", "BOX-2", "CN"]),
        ("D-004", ["S1", "P-02", "端盖", "A", "BOX-1", "CN"]),
        ("D-005", ["S3", "P-04", "垫圈", "C", "BOX-3", "CN"]),
    ]
    _write_table(root, "s8_duplicate_rows", columns, rows)


# --------------------------------------------------------------------------- #
# 场景 5/6：坏行与修好后的版本
# --------------------------------------------------------------------------- #
def scenario_bad_rows(root: Path) -> None:
    columns = DEMO_COLUMNS
    path = root / "columns.json"
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"table_id": "s5_bad_rows",
                                "columns": list(columns)},
                               ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    lines = [
        # 正常行
        {"row_id": "B-001", "values": {"supplier": "S1", "part": "P-01",
                                       "name": "轴承座", "grade": "A",
                                       "package": "BOX-1", "origin": "CN"}},
        # 少列：缺 package
        {"row_id": "B-002", "values": {"supplier": "S1", "part": "P-02",
                                       "name": "端盖", "grade": "A",
                                       "origin": "CN"}},
        # 多列：warranty 不在列清单
        {"row_id": "B-003", "values": {"supplier": "S2", "part": "P-01",
                                       "name": "轴承座", "grade": "B",
                                       "package": "BOX-2", "origin": "CN",
                                       "warranty": "12m"}},
        # 重复 row_id（与 B-001 重复）
        {"row_id": "B-001", "values": {"supplier": "S9", "part": "P-09",
                                       "name": "重复行", "grade": "C",
                                       "package": "BOX-9", "origin": "VN"}},
        # 值类型错误：grade 是数字
        {"row_id": "B-005", "values": {"supplier": "S3", "part": "P-04",
                                       "name": "垫圈", "grade": 3,
                                       "package": "BOX-3", "origin": "CN"}},
    ]
    with (root / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for record in lines:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def scenario_bad_rows_fixed(root: Path) -> None:
    columns = DEMO_COLUMNS
    rows = [
        ("B-001", ["S1", "P-01", "轴承座", "A", "BOX-1", "CN"]),
        ("B-002", ["S1", "P-02", "端盖", "A", "BOX-1", "CN"]),
        ("B-003", ["S2", "P-01", "轴承座", "B", "BOX-2", "CN"]),
        ("B-005", ["S3", "P-04", "垫圈", "3", "BOX-3", "CN"]),
    ]
    _write_table(root, "s6_bad_rows_fixed", columns, rows)


def scenario_typo_columns(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "columns.json").write_text(json.dumps(
        {"table_id": "s7_typo_columns",
         "columns": ["supplier", "part", "name", "part"]},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = [("T-001", ["S1", "P-01", "轴承座", "P-01"])]
    with (root / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for row_id, values in rows:
            fh.write(json.dumps({"row_id": row_id,
                                 "values": {"supplier": values[0], "part": values[1],
                                            "name": values[2]}},
                                ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# 规模样本（bench）
# --------------------------------------------------------------------------- #
BENCH_COLUMNS = [
    "supplier", "part", "name", "grade", "package", "origin",
    "line", "shift", "lot_prefix", "color", "finish", "unit",
]

# 每列的基数（含 null 的列用一个额外的 null 概率控制）
_BENCH_CARD = [8000, 3000, 3000, 40, 40, 25, 18, 12, 9, 7, 5, 3]
_BENCH_NULL_EVERY = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]


def _value_for(column_index: int, index: int) -> str:
    return f"V{column_index:02d}-{index:06d}"


def write_bench(root: Path, rows: int = 20_000) -> None:
    """确定性生成 12 列 × ``rows`` 行的规模样本（无需随机数）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "columns.json").write_text(json.dumps(
        {"table_id": "bench_supplier_wide", "columns": BENCH_COLUMNS},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 线性同余的确定性伪随机（与生产搜索无关，仅用于造数）
    state = 20261113
    modulus = 2**31 - 1
    multiplier = 1103515245
    increment = 12345

    def next_int(bound: int) -> int:
        nonlocal state
        state = (multiplier * state + increment) % modulus
        return state % bound

    with (root / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for row in range(rows):
            values = {}
            for c, card in enumerate(_BENCH_CARD):
                value_index = next_int(card)
                values[BENCH_COLUMNS[c]] = _value_for(c, value_index)
            fh.write(json.dumps({"row_id": f"B{row:07d}", "values": values},
                                ensure_ascii=False) + "\n")


SCENARIOS = {
    "demo": [
        ("s1_composite_key", scenario_composite_key),
        ("s2_null_policies", scenario_null_policies),
        ("s3_evidence_gap", scenario_evidence_gap),
        ("s4_violation_to_hold", scenario_violation_to_hold),
        ("s5_bad_rows", scenario_bad_rows),
        ("s6_bad_rows_fixed", scenario_bad_rows_fixed),
        ("s7_typo_columns", scenario_typo_columns),
        ("s8_duplicate_rows", scenario_duplicate_rows),
    ],
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成供货属性材料")
    parser.add_argument("--root", required=True, help="输出根目录")
    parser.add_argument("--scenario", choices=("demo", "bench", "all"),
                        default="demo")
    parser.add_argument("--rows", type=int, default=20_000,
                        help="bench 场景的行数（默认 20000）")
    args = parser.parse_args(argv)
    root = Path(args.root)

    if args.scenario in ("demo", "all"):
        for name, builder in SCENARIOS["demo"]:
            builder(root / name)
            print(f"wrote {root / name}")
    if args.scenario in ("bench", "all"):
        write_bench(root, args.rows)
        print(f"wrote {root} ({len(BENCH_COLUMNS)} 列 × {args.rows} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
