"""流程分析：直接跟随计数、路径变体、等待时长分位数、违规明细。

提供两套实现并交叉核对：
1. 纯 Python 参考实现（零第三方依赖，便于人工复核）；
2. PM4Py 适配器（事件日志 + DFG + 变体统计），两者结果必须一致，否则报错。
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .models import SyntheticBatch, WaitSample

# PM4Py 为可选重依赖；导入失败时纯 Python 路径仍可工作
try:
    import pandas as pd  # type: ignore
    import pm4py  # type: ignore
    from pm4py.algo.discovery.dfg import algorithm as dfg_discovery  # type: ignore
    from pm4py.algo.filtering.log.variants import (  # type: ignore
        variants_filter,
    )

    _PM4PY_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺失时的降级路径
    _PM4PY_AVAILABLE = False


# --------------------------------------------------------------------------- #
# 纯 Python 参考统计
# --------------------------------------------------------------------------- #
def direct_follow_counts(
    batches: Sequence[SyntheticBatch],
) -> Dict[Tuple[str, str], int]:
    """跨批次直接跟随计数：相邻活动 (a, b) 出现次数。"""
    counter: Counter[Tuple[str, str]] = Counter()
    for batch in batches:
        trace = batch.trace
        for a, b in zip(trace, trace[1:]):
            counter[(a, b)] += 1
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def path_variants(
    batches: Sequence[SyntheticBatch],
) -> List[dict]:
    """路径变体：完整活动序列 -> 出现批次数与批次清单。"""
    grouped: Dict[Tuple[str, ...], List[str]] = defaultdict(list)
    for batch in batches:
        grouped[batch.trace].append(batch.synthetic_batch_id)
    variants = [
        {
            "variant": list(variant),
            "frequency": len(ids),
            "synthetic_batch_ids": sorted(ids),
        }
        for variant, ids in grouped.items()
    ]
    variants.sort(key=lambda v: (-v["frequency"], v["variant"]))
    return variants


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """线性插值分位数（与 numpy 默认 'linear' 方法一致）。"""
    if not sorted_values:
        raise ValueError("空样本无法计算分位数")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(sorted_values[low])
    frac = rank - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


DEFAULT_PERCENTILES = (50, 75, 90, 95, 99)


def wait_quantiles(
    batches: Sequence[SyntheticBatch],
    percentiles: Tuple[float, ...] = DEFAULT_PERCENTILES,
) -> Dict[str, dict]:
    """按 operation 汇总等待时长（秒）分位数。等待定义见 state_machine.WaitSample。"""
    by_op: Dict[str, List[WaitSample]] = defaultdict(list)
    for batch in batches:
        for sample in batch.wait_samples:
            by_op[sample.operation].append(sample)

    result: Dict[str, dict] = {}
    for op in sorted(by_op):
        values = sorted(s.wait_seconds for s in by_op[op])
        entry = {
            "operation": op,
            "sample_count": len(values),
            "min_seconds": values[0],
            "max_seconds": values[-1],
            "mean_seconds": sum(values) / len(values),
            "percentiles": {f"p{int(p)}": _percentile(values, p) for p in percentiles},
        }
        result[op] = entry
    return result


def violation_rows(batches: Sequence[SyntheticBatch]) -> List[dict]:
    """违规明细打平为表，含阻断边与原始行位置。"""
    rows: List[dict] = []
    for batch in batches:
        for v in batch.violations:
            rows.append(v.to_dict())
    rows.sort(
        key=lambda r: (
            r["synthetic_batch_id"],
            r["row_number"] if r["row_number"] is not None else 10**12,
            r["violation_type"],
        )
    )
    return rows


def dangling_rows(batches: Sequence[SyntheticBatch]) -> List[dict]:
    """悬空操作明细（OPEN 无 CLOSE / CLOSE 无 OPEN）。"""
    rows: List[dict] = []
    for batch in batches:
        for raw, reason in batch.dangling:
            rows.append(
                {
                    "synthetic_batch_id": batch.synthetic_batch_id,
                    "reason": reason,
                    "source_event_id": raw.source_event_id,
                    "line_code": raw.line_code,
                    "operation": raw.operation,
                    "action": raw.action,
                    "row_number": raw.row_number,
                    "package_id": raw.package_id,
                }
            )
    return sorted(rows, key=lambda r: (r["synthetic_batch_id"], r["row_number"]))


# --------------------------------------------------------------------------- #
# PM4Py 适配器
# --------------------------------------------------------------------------- #
def build_pm4py_dataframe(batches: Sequence[SyntheticBatch]) -> "pd.DataFrame":
    """把合成批次事件转成 PM4Py 要求的扁平事件表。"""
    if not _PM4PY_AVAILABLE:
        raise RuntimeError("PM4Py 不可用，无法构建事件日志")
    records = []
    for batch in batches:
        for ev in batch.events:
            records.append(
                {
                    "case_id": batch.synthetic_batch_id,
                    "activity": f"{ev.raw.operation}:{ev.raw.action}",
                    "timestamp": ev.utc_time,
                    "line_code": ev.raw.line_code,
                    "source_event_id": ev.raw.source_event_id,
                }
            )
    df = pd.DataFrame(records)
    return pm4py.format_dataframe(
        df, case_id="case_id", activity_key="activity", timestamp_key="timestamp"
    )


def pm4py_statistics(batches: Sequence[SyntheticBatch]) -> dict:
    """用 PM4Py 计算 DFG 直接跟随与路径变体（轨迹统计入口）。"""
    if not _PM4PY_AVAILABLE:
        raise RuntimeError("PM4Py 不可用")
    df = build_pm4py_dataframe(batches)
    log = pm4py.convert_to_event_log(df)

    dfg = dfg_discovery.apply(log)
    dfg_counts = {tuple(k): int(v) for k, v in dfg.items()}

    variants = variants_filter.get_variants(log)
    variant_stats = [
        {
            "variant": list(variant),
            "frequency": len(traces),
            "synthetic_batch_ids": sorted(
                t.attributes["concept:name"] for t in traces
            ),
        }
        for variant, traces in variants.items()
    ]
    variant_stats.sort(key=lambda v: (-v["frequency"], v["variant"]))
    return {
        "dfg_counts": dict(sorted(dfg_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "variants": variant_stats,
        "trace_count": len(log),
        "event_count": sum(len(trace) for trace in log),
    }


def cross_check_with_pm4py(batches: Sequence[SyntheticBatch]) -> dict:
    """纯 Python 与 PM4Py 双路计算并断言一致。"""
    ref_dfg = direct_follow_counts(batches)
    ref_variants = path_variants(batches)

    if not _PM4PY_AVAILABLE:
        return {
            "pm4py_available": False,
            "dfg_match": None,
            "variants_match": None,
            "reference_dfg": ref_dfg,
            "reference_variants": ref_variants,
        }

    pm = pm4py_statistics(batches)
    dfg_match = pm["dfg_counts"] == ref_dfg
    if not dfg_match:
        raise AssertionError(
            f"DFG 统计不一致: pm4py={pm['dfg_counts']} reference={ref_dfg}"
        )

    pm_index = {tuple(v["variant"]): (v["frequency"], v["synthetic_batch_ids"])
                for v in pm["variants"]}
    ref_index = {tuple(v["variant"]): (v["frequency"], v["synthetic_batch_ids"])
                 for v in ref_variants}
    variants_match = pm_index == ref_index
    if not variants_match:
        raise AssertionError(
            f"变体统计不一致: pm4py={pm_index} reference={ref_index}"
        )

    return {
        "pm4py_available": True,
        "dfg_match": True,
        "variants_match": True,
        "trace_count": pm["trace_count"],
        "event_count": pm["event_count"],
    }


# --------------------------------------------------------------------------- #
# 汇总分析表
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AnalysisResult:
    direct_follow: Dict[Tuple[str, str], int]
    variants: List[dict]
    wait_quantiles: Dict[str, dict]
    violations: List[dict]
    dangling: List[dict]
    batch_summaries: List[dict]
    cross_check: dict

    def to_dict(self) -> dict:
        return {
            "direct_follow": [
                {"from_activity": a, "to_activity": b, "count": n}
                for (a, b), n in self.direct_follow.items()
            ],
            "path_variants": self.variants,
            "wait_quantiles": list(self.wait_quantiles.values()),
            "violations": self.violations,
            "dangling_operations": self.dangling,
            "batch_summaries": self.batch_summaries,
            "cross_check": self.cross_check,
        }


def analyze(batches: Sequence[SyntheticBatch]) -> AnalysisResult:
    """全量分析入口。"""
    summaries = [
        {
            "synthetic_batch_id": b.synthetic_batch_id,
            "members": sorted(f"{m.line_code}/{m.batch_label}" for m in b.members),
            "unassigned": b.unassigned,
            "unassigned_reason": b.unassigned_reason,
            "event_count": len(b.events),
            "trace": list(b.trace),
            "rework_count": b.rework_count,
            "violation_count": len(b.violations),
            "dangling_count": len(b.dangling),
        }
        for b in sorted(batches, key=lambda x: x.synthetic_batch_id)
    ]
    return AnalysisResult(
        direct_follow=direct_follow_counts(batches),
        variants=path_variants(batches),
        wait_quantiles=wait_quantiles(batches),
        violations=violation_rows(batches),
        dangling=dangling_rows(batches),
        batch_summaries=summaries,
        cross_check=cross_check_with_pm4py(batches),
    )
