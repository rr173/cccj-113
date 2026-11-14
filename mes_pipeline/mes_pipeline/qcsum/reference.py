"""仅供核对的参考实现：完整 LEFT JOIN / GROUP BY 重算五项汇总口径。

本模块不参与生产增量路径（`SummaryStore.apply_batch` 从不调用它）；
只被测试、`qcsum verify`、`qcsum demo` 与 `qcsum_demo/verify.py` 使用，
作为独立的全量重算来核对持久汇总与空组。
"""
from __future__ import annotations

SUMMARY_COLUMNS = ("supplier", "site", "joined_rows", "n_samples",
                   "n_lots", "n_grades", "rejected_sum")

#: 固定口径：lots LEFT JOIN samples ON lot_id，按 supplier、site 分组。
#: COUNT(*)              连接结果行数（无样本批次贡献一条占位行）
#: COUNT(s.sample_id)    实际样本数
#: COUNT(DISTINCT lot)   不同批次数
#: COUNT(DISTINCT grade) 不同非 null grade 数（SQL 标准忽略 NULL，空串算一个值）
#: SUM(rejected_units)   数量和；全部为非 null 缺失（或无样本）时为 NULL
REFERENCE_SQL = """\
SELECT l.supplier AS supplier, l.site AS site,
       COUNT(*) AS joined_rows,
       COUNT(s.sample_id) AS n_samples,
       COUNT(DISTINCT l.lot_id) AS n_lots,
       COUNT(DISTINCT s.grade) AS n_grades,
       SUM(s.rejected_units) AS rejected_sum
FROM lots AS l LEFT JOIN samples AS s ON s.lot_id = l.lot_id
GROUP BY l.supplier, l.site
ORDER BY l.supplier, l.site
"""


def reference_summary(conn):
    """在任意 sqlite3 连接上用全量连接重算汇总，返回 dict 列表。"""
    cur = conn.execute(REFERENCE_SQL)
    return [dict(zip(SUMMARY_COLUMNS, row)) for row in cur.fetchall()]


def diff_summary(expected, actual):
    """返回可读差异列表；空列表表示两侧一致（含空组情形）。"""
    exp = {(r["supplier"], r["site"]): r for r in expected}
    act = {(r["supplier"], r["site"]): r for r in actual}
    out = []
    for key in sorted(set(exp) | set(act)):
        if key not in exp:
            out.append(f"多出的组 {key}: {act[key]}")
        elif key not in act:
            out.append(f"缺失的组 {key}: {exp[key]}")
        elif exp[key] != act[key]:
            fields = {f: (exp[key][f], act[key][f])
                      for f in SUMMARY_COLUMNS if exp[key][f] != act[key][f]}
            out.append(f"组 {key} 不一致（期望, 实际）: {fields}")
    return out
