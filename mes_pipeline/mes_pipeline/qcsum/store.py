"""SQLite 持久化与增量维护核心：原表、辅助计数、汇总、成功账同一事务生效。

表结构：

- lots / samples        原表（不设外键、不设级联；允许样本引用尚未到达或已删除的批次）
- qc_sample_agg         每个被样本引用的 lot_id 的样本数/数量和/非空数量数（批次不存在也保留）
- qc_sample_grade_agg   每个 lot_id 每个非 null grade 的样本数
- qc_grade_counts       每组每个非 null grade 的在组样本数（n>0 才保留行）
- qc_summary            持久汇总（n_lots>=1 才保留行；rej_nonnull 为辅助列，不属结果列）
- qc_batches            成功账（batch_id → 内容哈希 + 既有结论）

增量规则（每条都可逆，删除即反向 delta）：

- 样本增删：先维护 qc_sample_agg / qc_sample_grade_agg（无论批次是否存在）；
  批次存在时再把 delta 传播到所在组。joined_rows 的 delta 为
  max(新样本数,1) - max(旧样本数,1)，保证无样本批次恰好贡献一条占位行。
- 批次建行/删除/换组：按该批次的 qc_sample_agg / qc_sample_grade_agg 整体
  从旧组减去、加到新组，只读该批次自己的样本邻域，不重扫 samples 表。
- 不同 grade 数：qc_grade_counts 行从 0 变正时 +1、变 0 删行时 -1，
  序列化集合与整组重扫都不需要。
- rejected_sum：qc_summary.rejected_sum 保存真实答案（全 null 时为 NULL），
  辅助列 rej_nonnull 记录非空数量数，为 0 时把和置回 NULL。

运行统计计数位置（_Stats 各字段在哪自增见代码注释）：

- lots_rows_read     _get_lot（存在性检查、取旧行、查样本所属批次分组）
- sample_rows_read   _get_sample（update/delete 取旧行）——即“读取的关联样本数”
- base_rows_written  原表每次 INSERT/UPDATE/DELETE
- aux_rows_read      四类辅助表每次 SELECT / fetchall 行数
- aux_rows_written   四类辅助表每次 INSERT/UPDATE/DELETE——即“调整的辅助键数”
- lots_touched       本批涉及的不同 lot_id（操作键与样本旧/新 lot_id）
- groups_touched     本批调整过的不同 (supplier, site) 组
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .errors import QcsumConflict, QcsumState, QcsumValidation
from .validate import canonical_hash, loads_strict, validate_batch, validate_row

SCHEMA = """\
CREATE TABLE IF NOT EXISTS lots (
    lot_id   TEXT PRIMARY KEY,
    supplier TEXT NOT NULL,
    site     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    sample_id      TEXT PRIMARY KEY,
    lot_id         TEXT NOT NULL,
    grade          TEXT,
    rejected_units INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_lot ON samples(lot_id);
CREATE TABLE IF NOT EXISTS qc_sample_agg (
    lot_id      TEXT PRIMARY KEY,
    n_samples   INTEGER NOT NULL,
    rej_sum     INTEGER NOT NULL,
    rej_nonnull INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS qc_sample_grade_agg (
    lot_id TEXT NOT NULL,
    grade  TEXT NOT NULL,
    n      INTEGER NOT NULL,
    PRIMARY KEY (lot_id, grade)
);
CREATE TABLE IF NOT EXISTS qc_grade_counts (
    supplier TEXT NOT NULL,
    site     TEXT NOT NULL,
    grade    TEXT NOT NULL,
    n        INTEGER NOT NULL,
    PRIMARY KEY (supplier, site, grade)
);
CREATE TABLE IF NOT EXISTS qc_summary (
    supplier     TEXT NOT NULL,
    site         TEXT NOT NULL,
    joined_rows  INTEGER NOT NULL,
    n_samples    INTEGER NOT NULL,
    n_lots       INTEGER NOT NULL,
    n_grades     INTEGER NOT NULL,
    rejected_sum INTEGER,
    rej_nonnull  INTEGER NOT NULL,
    PRIMARY KEY (supplier, site)
);
CREATE TABLE IF NOT EXISTS qc_batches (
    batch_id    TEXT PRIMARY KEY,
    batch_hash  TEXT NOT NULL,
    result_json TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);
"""

#: 可重复的故障注入点：
#: after_base   —— 原表已改、辅助状态尚未改完时真实终止进程（事务未提交）
#: after_commit —— 事务提交后、返回前真实终止进程
CRASH_POINTS = ("after_base", "after_commit")
CRASH_EXIT_CODE = 137

SUMMARY_COLUMNS = ("supplier", "site", "joined_rows", "n_samples",
                   "n_lots", "n_grades", "rejected_sum")

_LOAD_CHUNK = 10000


@dataclass
class _Stats:
    """单批运行统计；集合只装本批涉及的键，规模有界。"""

    ops_applied: int = 0
    lots_rows_read: int = 0
    sample_rows_read: int = 0
    base_rows_written: int = 0
    aux_rows_read: int = 0
    aux_rows_written: int = 0
    lots: set = field(default_factory=set)
    groups: set = field(default_factory=set)

    def as_dict(self, elapsed_ms):
        return {
            "ops_applied": self.ops_applied,
            "lots_touched": len(self.lots),
            "groups_touched": len(self.groups),
            "lots_rows_read": self.lots_rows_read,
            "sample_rows_read": self.sample_rows_read,
            "base_rows_written": self.base_rows_written,
            "aux_rows_read": self.aux_rows_read,
            "aux_rows_written": self.aux_rows_written,
            "elapsed_ms": round(elapsed_ms, 3),
        }


class SummaryStore:
    """质检汇总库：初始化、装载初始两表、应用更改批次、读取持久汇总与原表。"""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        # 手动事务模式：所有写入都在显式 BEGIN IMMEDIATE ... COMMIT 中
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------ 装载

    def load_initial(self, lots, samples):
        """空库一次性装载两张原表并建立全部辅助计数与汇总（唯一一次全量扫描）。"""
        cur = self.conn.cursor()
        state = cur.execute(
            "SELECT (SELECT COUNT(*) FROM lots) AS nl,"
            "       (SELECT COUNT(*) FROM samples) AS ns,"
            "       (SELECT COUNT(*) FROM qc_batches) AS nb").fetchone()
        if state["nl"] or state["ns"] or state["nb"]:
            raise QcsumState(
                "load_initial 只接受空库：lots/samples/qc_batches 必须全部为空")
        t0 = time.perf_counter()
        n_lots = 0
        n_samples = 0
        cur.execute("BEGIN IMMEDIATE")
        try:
            chunk = []
            for idx, row in enumerate(lots, 1):
                validate_row("lots", row, where=f"lots 第 {idx} 行")
                chunk.append((row["lot_id"], row["supplier"], row["site"]))
                if len(chunk) >= _LOAD_CHUNK:
                    self._load_chunk(cur, "lots", chunk)
                    n_lots += len(chunk)
                    chunk = []
            if chunk:
                self._load_chunk(cur, "lots", chunk)
                n_lots += len(chunk)
            chunk = []
            for idx, row in enumerate(samples, 1):
                validate_row("samples", row, where=f"samples 第 {idx} 行")
                chunk.append((row["sample_id"], row["lot_id"],
                              row["grade"], row["rejected_units"]))
                if len(chunk) >= _LOAD_CHUNK:
                    self._load_chunk(cur, "samples", chunk)
                    n_samples += len(chunk)
                    chunk = []
            if chunk:
                self._load_chunk(cur, "samples", chunk)
                n_samples += len(chunk)
            self._rebuild_aux(cur)
            cur.execute("COMMIT")
        except BaseException:
            try:
                cur.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        elapsed = (time.perf_counter() - t0) * 1000
        return {"status": "loaded", "lots_loaded": n_lots,
                "samples_loaded": n_samples, "elapsed_ms": round(elapsed, 3)}

    @staticmethod
    def _load_chunk(cur, table, chunk):
        sql = ("INSERT INTO lots(lot_id, supplier, site) VALUES (?,?,?)" if table == "lots"
               else "INSERT INTO samples(sample_id, lot_id, grade, rejected_units)"
                    " VALUES (?,?,?,?)")
        try:
            cur.executemany(sql, chunk)
        except sqlite3.IntegrityError as exc:
            raise QcsumValidation(
                f"{table} 装载违反主键唯一约束（重复主键）: {exc}") from None

    @staticmethod
    def _rebuild_aux(cur):
        """从两张原表一次性建立辅助计数与汇总（仅装载时调用）。"""
        cur.execute(
            "INSERT INTO qc_sample_agg(lot_id, n_samples, rej_sum, rej_nonnull)"
            " SELECT lot_id, COUNT(*), COALESCE(SUM(rejected_units), 0),"
            "        COUNT(rejected_units)"
            " FROM samples GROUP BY lot_id")
        cur.execute(
            "INSERT INTO qc_sample_grade_agg(lot_id, grade, n)"
            " SELECT lot_id, grade, COUNT(*) FROM samples"
            " WHERE grade IS NOT NULL GROUP BY lot_id, grade")
        cur.execute(
            "INSERT INTO qc_summary(supplier, site, joined_rows, n_samples, n_lots,"
            "                       n_grades, rejected_sum, rej_nonnull)"
            " SELECT l.supplier, l.site,"
            "        SUM(MAX(COALESCE(a.n_samples, 0), 1)),"
            "        COALESCE(SUM(a.n_samples), 0),"
            "        COUNT(*), 0,"
            "        CASE WHEN COALESCE(SUM(a.rej_nonnull), 0) > 0"
            "             THEN SUM(a.rej_sum) ELSE NULL END,"
            "        COALESCE(SUM(a.rej_nonnull), 0)"
            " FROM lots l LEFT JOIN qc_sample_agg a ON a.lot_id = l.lot_id"
            " GROUP BY l.supplier, l.site")
        cur.execute(
            "INSERT INTO qc_grade_counts(supplier, site, grade, n)"
            " SELECT l.supplier, l.site, g.grade, SUM(g.n)"
            " FROM lots l JOIN qc_sample_grade_agg g ON g.lot_id = l.lot_id"
            " GROUP BY l.supplier, l.site, g.grade")
        cur.execute(
            "UPDATE qc_summary SET n_grades = ("
            " SELECT COUNT(*) FROM qc_grade_counts c"
            " WHERE c.supplier = qc_summary.supplier"
            "   AND c.site = qc_summary.site)")

    # -------------------------------------------------------------- 更改批次

    def apply_batch(self, batch, *, crash_at=None):
        """应用一批有序更改。全部表与成功账在同一事务中生效。

        batch 可以是 dict 或 JSON 文本（文本走严格解析，重复对象键拒绝）。
        crash_at 为可重复故障注入点，见 CRASH_POINTS；仅用于故障演练。
        已成功批次原内容重送返回既有结论（status=replayed），不重复加减；
        同号改内容抛 QcsumConflict。
        """
        if crash_at is not None and crash_at not in CRASH_POINTS:
            raise QcsumValidation(
                f"未知故障注入点 {crash_at!r}，可选: {list(CRASH_POINTS)}")
        if isinstance(batch, (str, bytes, bytearray)):
            batch = loads_strict(batch)
        batch_id, ops = validate_batch(batch)
        digest = canonical_hash(batch)
        cur = self.conn.cursor()
        ledger = cur.execute(
            "SELECT batch_hash, result_json FROM qc_batches WHERE batch_id=?",
            (batch_id,)).fetchone()
        if ledger is not None:
            if ledger["batch_hash"] != digest:
                raise QcsumConflict(
                    f"批次号 {batch_id!r} 已成功登记，新送内容与其不一致（冲突）")
            result = json.loads(ledger["result_json"])
            result["status"] = "replayed"
            return result
        st = _Stats()
        t0 = time.perf_counter()
        cur.execute("BEGIN IMMEDIATE")
        try:
            for op in ops:
                self._apply_op(cur, op, st, crash_at)
                st.ops_applied += 1
            elapsed = (time.perf_counter() - t0) * 1000
            result = {"batch_id": batch_id, "status": "applied",
                      "ops_applied": st.ops_applied, "stats": st.as_dict(elapsed)}
            cur.execute(
                "INSERT INTO qc_batches(batch_id, batch_hash, result_json, applied_at)"
                " VALUES (?,?,?,?)",
                (batch_id, digest,
                 json.dumps(result, ensure_ascii=False, sort_keys=True),
                 datetime.now(timezone.utc).isoformat()))
            cur.execute("COMMIT")
        except BaseException:
            try:
                cur.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        self._maybe_crash(crash_at, "after_commit")
        return result

    @staticmethod
    def _maybe_crash(crash_at, point):
        """真实终止进程（不提交、不清理），模拟 kill；仅故障演练使用。"""
        if crash_at == point:
            os._exit(CRASH_EXIT_CODE)

    def _apply_op(self, cur, op, st, crash_at):
        if op["table"] == "lots":
            self._apply_lots(cur, op, st, crash_at)
        else:
            self._apply_samples(cur, op, st, crash_at)

    # ------------------------------------------------------------ 原表操作

    def _get_lot(self, cur, lot_id, st):
        st.lots_rows_read += 1
        return cur.execute(
            "SELECT supplier, site FROM lots WHERE lot_id=?", (lot_id,)).fetchone()

    def _get_sample(self, cur, sample_id, st):
        st.sample_rows_read += 1
        return cur.execute(
            "SELECT lot_id, grade, rejected_units FROM samples WHERE sample_id=?",
            (sample_id,)).fetchone()

    def _apply_lots(self, cur, op, st, crash_at):
        kind, key, row = op["op"], op["key"], op.get("row")
        old = self._get_lot(cur, key, st)
        st.lots.add(key)
        if kind == "insert":
            if old is not None:
                raise QcsumValidation(f"insert 要求键不存在：lots 主键 {key!r} 已存在")
            cur.execute("INSERT INTO lots(lot_id, supplier, site) VALUES (?,?,?)",
                        (row["lot_id"], row["supplier"], row["site"]))
            st.base_rows_written += 1
            self._maybe_crash(crash_at, "after_base")
            self._lot_join_group(cur, key, row["supplier"], row["site"], +1, st)
        elif kind == "update":
            if old is None:
                raise QcsumValidation(f"update 要求键存在：lots 主键 {key!r} 不存在")
            cur.execute("UPDATE lots SET supplier=?, site=? WHERE lot_id=?",
                        (row["supplier"], row["site"], key))
            st.base_rows_written += 1
            self._maybe_crash(crash_at, "after_base")
            old_group = (old["supplier"], old["site"])
            new_group = (row["supplier"], row["site"])
            if old_group != new_group:
                self._lot_join_group(cur, key, old_group[0], old_group[1], -1, st)
                self._lot_join_group(cur, key, new_group[0], new_group[1], +1, st)
        else:  # delete
            if old is None:
                raise QcsumValidation(f"delete 要求键存在：lots 主键 {key!r} 不存在")
            cur.execute("DELETE FROM lots WHERE lot_id=?", (key,))
            st.base_rows_written += 1
            self._maybe_crash(crash_at, "after_base")
            self._lot_join_group(cur, key, old["supplier"], old["site"], -1, st)

    def _apply_samples(self, cur, op, st, crash_at):
        kind, key, row = op["op"], op["key"], op.get("row")
        old = self._get_sample(cur, key, st)
        if kind == "insert":
            if old is not None:
                raise QcsumValidation(f"insert 要求键不存在：samples 主键 {key!r} 已存在")
            cur.execute(
                "INSERT INTO samples(sample_id, lot_id, grade, rejected_units)"
                " VALUES (?,?,?,?)",
                (row["sample_id"], row["lot_id"], row["grade"], row["rejected_units"]))
            st.base_rows_written += 1
            self._maybe_crash(crash_at, "after_base")
            self._sample_delta(cur, row["lot_id"], row["grade"],
                               row["rejected_units"], +1, st)
        elif kind == "update":
            if old is None:
                raise QcsumValidation(f"update 要求键存在：samples 主键 {key!r} 不存在")
            cur.execute(
                "UPDATE samples SET lot_id=?, grade=?, rejected_units=?"
                " WHERE sample_id=?",
                (row["lot_id"], row["grade"], row["rejected_units"], key))
            st.base_rows_written += 1
            self._maybe_crash(crash_at, "after_base")
            self._sample_delta(cur, old["lot_id"], old["grade"],
                               old["rejected_units"], -1, st)
            self._sample_delta(cur, row["lot_id"], row["grade"],
                               row["rejected_units"], +1, st)
        else:  # delete
            if old is None:
                raise QcsumValidation(f"delete 要求键存在：samples 主键 {key!r} 不存在")
            cur.execute("DELETE FROM samples WHERE sample_id=?", (key,))
            st.base_rows_written += 1
            self._maybe_crash(crash_at, "after_base")
            self._sample_delta(cur, old["lot_id"], old["grade"],
                               old["rejected_units"], -1, st)

    # ------------------------------------------------------------ 增量维护

    def _sample_delta(self, cur, lot_id, grade, rejected, sign, st):
        """一条样本对辅助计数与所在组的 delta（sign=+1 加入 / -1 撤销）。"""
        st.lots.add(lot_id)
        row = cur.execute(
            "SELECT n_samples, rej_sum, rej_nonnull FROM qc_sample_agg"
            " WHERE lot_id=?", (lot_id,)).fetchone()
        st.aux_rows_read += 1
        old_n = row["n_samples"] if row else 0
        new_n = old_n + sign
        if new_n < 0:
            raise QcsumState(f"辅助计数不变量被破坏：{lot_id!r} 的样本数变负")
        new_sum = (row["rej_sum"] if row else 0) + sign * (rejected or 0)
        new_non = (row["rej_nonnull"] if row else 0) + sign * (rejected is not None)
        if new_n == 0:
            if row is not None:
                cur.execute("DELETE FROM qc_sample_agg WHERE lot_id=?", (lot_id,))
                st.aux_rows_written += 1
        elif row is not None:
            cur.execute(
                "UPDATE qc_sample_agg SET n_samples=?, rej_sum=?, rej_nonnull=?"
                " WHERE lot_id=?", (new_n, new_sum, new_non, lot_id))
            st.aux_rows_written += 1
        else:
            cur.execute(
                "INSERT INTO qc_sample_agg(lot_id, n_samples, rej_sum, rej_nonnull)"
                " VALUES (?,?,?,?)", (lot_id, new_n, new_sum, new_non))
            st.aux_rows_written += 1
        if grade is not None:
            self._agg_grade_add(cur, lot_id, grade, sign, st)
        lot = self._get_lot(cur, lot_id, st)
        if lot is None:
            return  # 孤立样本：只记样本侧聚合，批次到达时再接回
        joined_delta = max(new_n, 1) - max(old_n, 1)
        self._group_delta(
            cur, lot["supplier"], lot["site"], st,
            d_lots=0, d_joined=joined_delta, d_samples=sign,
            d_rej_sum=sign * (rejected or 0),
            d_rej_nonnull=sign * (rejected is not None))
        if grade is not None:
            self._grade_count_add(cur, lot["supplier"], lot["site"], grade, sign, st)

    def _agg_grade_add(self, cur, lot_id, grade, sign, st):
        """维护 qc_sample_grade_agg（每个 lot_id 每个非 null grade 的样本数）。"""
        row = cur.execute(
            "SELECT n FROM qc_sample_grade_agg WHERE lot_id=? AND grade=?",
            (lot_id, grade)).fetchone()
        st.aux_rows_read += 1
        old = row["n"] if row else 0
        new = old + sign
        if new < 0:
            raise QcsumState(
                f"辅助计数不变量被破坏：({lot_id!r}, {grade!r}) 的样本数变负")
        if new == 0:
            cur.execute("DELETE FROM qc_sample_grade_agg"
                        " WHERE lot_id=? AND grade=?", (lot_id, grade))
        elif row is not None:
            cur.execute("UPDATE qc_sample_grade_agg SET n=?"
                        " WHERE lot_id=? AND grade=?", (new, lot_id, grade))
        else:
            cur.execute("INSERT INTO qc_sample_grade_agg(lot_id, grade, n)"
                        " VALUES (?,?,?)", (lot_id, grade, new))
        st.aux_rows_written += 1

    def _lot_join_group(self, cur, lot_id, supplier, site, sign, st):
        """批次整批进入（+1）或离开（-1）某组：只读该批自己的样本邻域。"""
        row = cur.execute(
            "SELECT n_samples, rej_sum, rej_nonnull FROM qc_sample_agg"
            " WHERE lot_id=?", (lot_id,)).fetchone()
        st.aux_rows_read += 1
        k = row["n_samples"] if row else 0
        rej_sum = row["rej_sum"] if row else 0
        rej_non = row["rej_nonnull"] if row else 0
        grades = cur.execute(
            "SELECT grade, n FROM qc_sample_grade_agg WHERE lot_id=?",
            (lot_id,)).fetchall()
        st.aux_rows_read += len(grades)
        if sign > 0:
            self._group_delta(
                cur, supplier, site, st,
                d_lots=1, d_joined=max(k, 1), d_samples=k,
                d_rej_sum=rej_sum, d_rej_nonnull=rej_non)
            for g in grades:
                self._grade_count_add(cur, supplier, site, g["grade"], g["n"], st)
        else:
            for g in grades:
                self._grade_count_add(cur, supplier, site, g["grade"], -g["n"], st)
            self._group_delta(
                cur, supplier, site, st,
                d_lots=-1, d_joined=-max(k, 1), d_samples=-k,
                d_rej_sum=-rej_sum, d_rej_nonnull=-rej_non)

    def _grade_count_add(self, cur, supplier, site, grade, delta, st):
        """维护 qc_grade_counts；0↔正 的跳变同步调整组内不同 grade 数。"""
        row = cur.execute(
            "SELECT n FROM qc_grade_counts"
            " WHERE supplier=? AND site=? AND grade=?",
            (supplier, site, grade)).fetchone()
        st.aux_rows_read += 1
        old = row["n"] if row else 0
        new = old + delta
        if new < 0:
            raise QcsumState(
                f"辅助计数不变量被破坏：组 ({supplier!r}, {site!r}) 的"
                f" grade {grade!r} 计数变负")
        if new == 0:
            cur.execute("DELETE FROM qc_grade_counts"
                        " WHERE supplier=? AND site=? AND grade=?",
                        (supplier, site, grade))
            self._ngrades_add(cur, supplier, site, -1, st)
        elif old == 0:
            cur.execute("INSERT INTO qc_grade_counts(supplier, site, grade, n)"
                        " VALUES (?,?,?,?)", (supplier, site, grade, new))
            self._ngrades_add(cur, supplier, site, +1, st)
        else:
            cur.execute("UPDATE qc_grade_counts SET n=?"
                        " WHERE supplier=? AND site=? AND grade=?",
                        (new, supplier, site, grade))
        st.aux_rows_written += 1

    def _ngrades_add(self, cur, supplier, site, delta, st):
        res = cur.execute(
            "UPDATE qc_summary SET n_grades = n_grades + ?"
            " WHERE supplier=? AND site=?", (delta, supplier, site))
        if res.rowcount != 1:
            raise QcsumState(
                f"辅助计数不变量被破坏：组 ({supplier!r}, {site!r}) 的汇总行缺失")
        st.aux_rows_written += 1

    def _group_delta(self, cur, supplier, site, st, *, d_lots, d_joined,
                     d_samples, d_rej_sum, d_rej_nonnull):
        """对一组的五项汇总施加 delta；最后一个批次消失时整组删除。"""
        st.groups.add((supplier, site))
        row = cur.execute(
            "SELECT joined_rows, n_samples, n_lots, n_grades, rejected_sum,"
            "       rej_nonnull FROM qc_summary"
            " WHERE supplier=? AND site=?", (supplier, site)).fetchone()
        st.aux_rows_read += 1
        if row is None:
            if d_lots <= 0:
                raise QcsumState(
                    f"辅助计数不变量被破坏：组 ({supplier!r}, {site!r}) 不存在")
            cur.execute(
                "INSERT INTO qc_summary(supplier, site, joined_rows, n_samples,"
                " n_lots, n_grades, rejected_sum, rej_nonnull)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (supplier, site, d_joined, d_samples, d_lots, 0,
                 d_rej_sum if d_rej_nonnull > 0 else None, d_rej_nonnull))
            st.aux_rows_written += 1
            return
        n_lots = row["n_lots"] + d_lots
        joined = row["joined_rows"] + d_joined
        n_samples = row["n_samples"] + d_samples
        nonnull = row["rej_nonnull"] + d_rej_nonnull
        rej_sum = ((row["rejected_sum"] or 0) + d_rej_sum) if nonnull > 0 else None
        if n_lots == 0:
            if joined != 0 or n_samples != 0 or nonnull != 0 or row["n_grades"] != 0:
                raise QcsumState(
                    f"辅助计数不变量被破坏：组 ({supplier!r}, {site!r})"
                    " 最后一个批次消失但计数未归零")
            cur.execute("DELETE FROM qc_summary WHERE supplier=? AND site=?",
                        (supplier, site))
            st.aux_rows_written += 1
            return
        if n_lots < 0 or joined < 0 or n_samples < 0 or nonnull < 0:
            raise QcsumState(
                f"辅助计数不变量被破坏：组 ({supplier!r}, {site!r}) 计数变负")
        cur.execute(
            "UPDATE qc_summary SET joined_rows=?, n_samples=?, n_lots=?,"
            " rejected_sum=?, rej_nonnull=?"
            " WHERE supplier=? AND site=?",
            (joined, n_samples, n_lots, rej_sum, nonnull, supplier, site))
        st.aux_rows_written += 1

    # ------------------------------------------------------------------ 读取

    def summary(self):
        """读取已维护好的持久汇总（直接 SELECT，不临时重算）。"""
        rows = self.conn.execute(
            "SELECT supplier, site, joined_rows, n_samples, n_lots, n_grades,"
            "       rejected_sum FROM qc_summary ORDER BY supplier, site").fetchall()
        return [dict(r) for r in rows]

    def base_rows(self, table):
        """读取原表当前内容。"""
        if table == "lots":
            rows = self.conn.execute(
                "SELECT lot_id, supplier, site FROM lots ORDER BY lot_id").fetchall()
        elif table == "samples":
            rows = self.conn.execute(
                "SELECT sample_id, lot_id, grade, rejected_units FROM samples"
                " ORDER BY sample_id").fetchall()
        else:
            raise QcsumValidation(f"未知原表 {table!r}，可选: lots / samples")
        return [dict(r) for r in rows]

    def batches(self):
        """读取成功账。"""
        rows = self.conn.execute(
            "SELECT batch_id, batch_hash, applied_at FROM qc_batches"
            " ORDER BY batch_id").fetchall()
        return [dict(r) for r in rows]
