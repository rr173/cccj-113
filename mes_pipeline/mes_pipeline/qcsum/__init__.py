"""质检仓库增量关系汇总模块（SQLite 持久化，纯标准库）。

用法概览：

    from mes_pipeline.qcsum import SummaryStore

    store = SummaryStore("qcsum.db")          # 初始化库（幂等建模式）
    store.load_initial(lots, samples)         # 空库装载初始两表
    result = store.apply_batch(batch)         # 应用一批有序更改（dict 或 JSON 文本）
    rows = store.summary()                    # 读取持久汇总
    lots = store.base_rows("lots")            # 读取原表
    ledger = store.batches()                  # 读取成功账
"""
from .errors import QcsumConflict, QcsumError, QcsumState, QcsumValidation
from .store import CRASH_POINTS, SUMMARY_COLUMNS, SummaryStore
from .validate import canonical_hash, loads_strict, validate_batch, validate_row

__all__ = [
    "SummaryStore",
    "QcsumError",
    "QcsumValidation",
    "QcsumConflict",
    "QcsumState",
    "CRASH_POINTS",
    "SUMMARY_COLUMNS",
    "loads_strict",
    "validate_batch",
    "validate_row",
    "canonical_hash",
]
