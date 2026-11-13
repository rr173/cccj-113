"""量具 R&R 分析编排：载入试验、计算、结果落盘与读取。

计算口径（与需求逐条对应）：
- 只接受完整平衡交叉设计（≥3 零件、≥2 操作员、每格 ≥2 次重复且无缺格）；
- 统计采用带零件×操作员交互的随机效应双因素 ANOVA，交互检验以误差均方
  为分母；p < alpha 保留交互，否则合并入误差后重新估计方差分量；
- 负方差分量保留原估计（raw），进入指标前各自独立截到零（used）；
- 重复性 = 误差方差，再现性 = 操作员 + 保留交互的方差，GRR = 两者之和，
  总变差 = GRR + 零件方差；
- 分母为零的比值明确标注不可定义及原因，不写成 0、无穷或可用结论；
- 结果保留零件/操作员/交叉格均值与原始行定位，可沿均方回溯到测量行。

失败分类（结果 JSON 的 status 字段，均落盘可读）：
- ``INVALID_INPUT``      输入错误（解析/单位/有限性/声明外 ID/重复行/规格/alpha）
- ``INCOMPLETE_DESIGN``  设计不完整（规模不足/缺格）
- ``OK``                 计算完成，全部统计量可定义
- ``OK_PARTIAL``         计算完成，个别统计量不可定义（其余已保存，见
                         undefined_statistics 汇总）
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .anova import (
    AnovaResult,
    build_metrics,
    collect_undefined,
    run_anova,
    ss_decomposition_ok,
)
from .io import GageDesignError, GageInputError, digest_files, load_gage_input
from .models import GageInput, Observation

CALC_DIR = "gage_rr"

STATUS_OK = "OK"
STATUS_PARTIAL = "OK_PARTIAL"
STATUS_INVALID = "INVALID_INPUT"
STATUS_INCOMPLETE = "INCOMPLETE_DESIGN"

DEFAULT_ALPHA = 0.25  # AIAG 惯例


# 统计数值一律输出完整浮点精度：方差分量可能非常小（1e-7 量级），
# 固定小数位舍入会把非零值显示成 0.0，违背“不写成零”的口径。


# --------------------------------------------------------------------------- #
# 均值与行定位（沿均方回溯到分组均值与测量行）
# --------------------------------------------------------------------------- #
def _group_means(gage: GageInput) -> Dict[str, object]:
    obs = gage.observations
    unit = gage.study.unit

    def pack(grouped: Dict[str, List[Observation]], key_name: str) -> List[dict]:
        out = []
        for key in sorted(grouped):
            rows = grouped[key]
            mean = math.fsum(o.canonical_value for o in rows) / len(rows)
            out.append({
                key_name: key,
                "mean": mean,
                "n": len(rows),
                "unit": unit,
                "rows": sorted(o.row for o in rows),
            })
        return out

    by_part: Dict[str, List[Observation]] = {}
    by_op: Dict[str, List[Observation]] = {}
    by_cell: Dict[Tuple[str, str], List[Observation]] = {}
    for o in obs:
        by_part.setdefault(o.part_id, []).append(o)
        by_op.setdefault(o.operator_id, []).append(o)
        by_cell.setdefault((o.part_id, o.operator_id), []).append(o)

    grand = math.fsum(o.canonical_value for o in obs) / len(obs)
    cells = []
    for key in sorted(by_cell):
        rows = by_cell[key]
        mean = math.fsum(o.canonical_value for o in rows) / len(rows)
        cells.append({
            "part_id": key[0],
            "operator_id": key[1],
            "mean": mean,
            "n": len(rows),
            "unit": unit,
            "rows": sorted(o.row for o in rows),
        })

    return {
        "grand": {"mean": grand, "n": len(obs), "unit": unit},
        "by_part": pack(by_part, "part_id"),
        "by_operator": pack(by_op, "operator_id"),
        "cells": cells,
    }


def _observation_rows(gage: GageInput) -> List[dict]:
    """原始行定位：按 (零件, 操作员, 重复) 排序，输出与输入行序无关。"""
    rows = sorted(gage.observations,
                  key=lambda o: (o.part_id, o.operator_id, o.repeat_id))
    return [
        {
            "row": o.row,
            "part_id": o.part_id,
            "operator_id": o.operator_id,
            "repeat_id": o.repeat_id,
            "value": o.value,
            "unit": o.unit,
            "canonical_value": o.canonical_value,
        }
        for o in rows
    ]


# --------------------------------------------------------------------------- #
# 结果组装
# --------------------------------------------------------------------------- #
def _term_dict(ss: float, df: int, ms: Optional[float]) -> dict:
    out = {"ss": ss, "df": df}
    if ms is not None:
        out["ms"] = ms
    return out


def _anova_dict(result: AnovaResult) -> dict:
    test = result.test
    interaction_test = {
        "alpha": test.alpha,
        "f_statistic": test.f_statistic,
        "df_num": test.df_num,
        "df_den": test.df_den,
        "p_value": test.p_value,
        "decision": test.decision,
        "reason": test.reason,
    }
    terms = {
        "part": _term_dict(result.part.ss, result.part.df, result.part.ms),
        "operator": _term_dict(result.operator.ss, result.operator.df,
                               result.operator.ms),
        "interaction": _term_dict(result.interaction.ss, result.interaction.df,
                                  result.interaction.ms),
        "error": _term_dict(result.error.ss, result.error.df, result.error.ms),
        "total": _term_dict(result.total.ss, result.total.df, None),
    }
    out: Dict[str, object] = {
        "model": "two-way random-effects ANOVA with part×operator interaction "
                 "(balanced crossed design)",
        "terms": terms,
        "ss_decomposition_check": {
            "ss_total": result.total.ss,
            "ss_sum_of_terms": (result.part.ss + result.operator.ss
                                   + result.interaction.ss + result.error.ss),
            "abs_diff": result.ss_check_diff,
            "ok": ss_decomposition_ok(result),
        },
        "interaction_test": interaction_test,
    }
    if result.pooled_error is not None:
        out["pooled_error"] = _term_dict(result.pooled_error.ss,
                                         result.pooled_error.df,
                                         result.pooled_error.ms)
    return out


def _components_dict(result: AnovaResult) -> dict:
    comp = result.components
    return {
        "branch": comp.branch,
        "raw": dict(comp.raw),
        "used": dict(comp.used),
        "truncated_to_zero": list(comp.truncated),
        "note": "raw 为原估计（可为负，保留供解释）；进入 GRR/总变差/比例指标前"
                "各分量独立截到零（used），不按比例分摊、不转嫁其他分量。",
    }


# --------------------------------------------------------------------------- #
# 入口：载入、计算、落盘
# --------------------------------------------------------------------------- #
def _sanitize_calc_id(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text)


def _result_path(store_dir: str | Path, calc_id: str) -> Path:
    return Path(store_dir) / CALC_DIR / f"{calc_id}.json"


def _save(store_dir: str | Path, calc_id: str, result: dict) -> None:
    path = _result_path(store_dir, calc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _failure(store_dir: str | Path, calc_id: str, status: str, base: dict,
             problems: List[str], note: str) -> dict:
    result = {
        **base,
        "calc_id": calc_id,
        "status": status,
        "errors": list(problems),
        "note": note,
    }
    _save(store_dir, calc_id, result)
    return result


def run_study(study: str | Path, observations: str | Path,
              store_dir: str | Path, alpha: float = DEFAULT_ALPHA,
              calc_id: Optional[str] = None) -> dict:
    """载入试验、计算并保存结果；任何状态都产出可读的结果 JSON。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    files = [Path(study), Path(observations)]
    try:
        digest = digest_files(files)
    except OSError as exc:
        calc_id = _sanitize_calc_id(calc_id or f"invalid-{stamp}")
        return _failure(store_dir, calc_id, STATUS_INVALID, {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "input": {"study": str(study), "observations": str(observations),
                      "sha256": None},
            "alpha": alpha,
        }, [f"输入文件不可读: {exc}"],
            "输入数据不合法，本次分析被拒绝。")

    base = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input": {
            "study": str(study),
            "observations": str(observations),
            "sha256": digest,
        },
        "alpha": alpha,
    }

    if not (isinstance(alpha, (int, float)) and not isinstance(alpha, bool)
            and math.isfinite(float(alpha)) and 0.0 < float(alpha) < 1.0):
        calc_id = _sanitize_calc_id(calc_id or f"invalid-{stamp}")
        return _failure(store_dir, calc_id, STATUS_INVALID, base,
                        [f"alpha 必须在 (0,1) 内: {alpha!r}"],
                        "输入数据不合法，本次分析被拒绝。")
    alpha = float(alpha)

    try:
        gage = load_gage_input(study, observations)
    except GageInputError as exc:
        calc_id = _sanitize_calc_id(calc_id or f"invalid-{stamp}")
        return _failure(store_dir, calc_id, STATUS_INVALID, base, exc.problems,
                        "输入数据不合法，本次分析被拒绝；缺值未补齐、重复行未平均。")
    except GageDesignError as exc:
        calc_id = _sanitize_calc_id(calc_id or f"incomplete-{stamp}")
        return _failure(store_dir, calc_id, STATUS_INCOMPLETE, base, exc.problems,
                        "试验设计不满足完整平衡交叉要求，本次分析被拒绝；"
                        "缺格未补齐、未悄悄少算任何零件或操作员。")

    spec = gage.study
    calc_id = _sanitize_calc_id(calc_id or f"{spec.study_id}-{stamp}")

    cell_values: Dict[Tuple[str, str], List[float]] = {}
    for o in gage.observations:
        cell_values.setdefault((o.part_id, o.operator_id), []).append(
            o.canonical_value)

    result = run_anova(cell_values, spec.parts, spec.operators, spec.repeats,
                       alpha)
    metrics = build_metrics(result.components, spec.spec_lower,
                        spec.spec_upper)
    undefined_stats = collect_undefined(metrics)
    status = STATUS_PARTIAL if undefined_stats else STATUS_OK

    payload = {
        **base,
        "calc_id": calc_id,
        "status": status,
        "study_id": spec.study_id,
        "characteristic": spec.characteristic,
        "design": {
            "parts": list(spec.parts),
            "operators": list(spec.operators),
            "repeats": spec.repeats,
            "n_parts": len(spec.parts),
            "n_operators": len(spec.operators),
            "n_cells": len(spec.parts) * len(spec.operators),
            "n_observations": len(gage.observations),
            "unit": spec.unit,
        },
        "spec": (None if spec.spec_width is None else {
            "lower": spec.spec_lower,
            "upper": spec.spec_upper,
            "width": spec.spec_width,
            "unit": spec.unit,
        }),
        "anova": _anova_dict(result),
        "variance_components": _components_dict(result),
        "metrics": metrics,
        "means": _group_means(gage),
        "observations": _observation_rows(gage),
    }
    if undefined_stats:
        payload["undefined_statistics"] = undefined_stats
        payload["note"] = ("个别统计量因分母为零而不可定义（见 "
                           "undefined_statistics），其余计算结果完整保存；"
                           "未写成 0、无穷或可用结论。")
    _save(store_dir, calc_id, payload)
    return payload


def load_result(store_dir: str | Path, calc_id: str) -> dict:
    """按 calc_id 读取某次分析结果（成功/部分成功/失败均可读）。"""
    path = _result_path(store_dir, calc_id)
    if not path.exists():
        known = sorted(p.stem for p in path.parent.glob("*.json")) \
            if path.parent.exists() else []
        raise KeyError(f"未找到分析结果 {calc_id!r}（已有: {known}）")
    return json.loads(path.read_text(encoding="utf-8"))
