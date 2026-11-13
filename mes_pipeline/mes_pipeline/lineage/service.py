"""谱系计算编排：装载、精确序列化、结果落盘/读取、构成查询与召回测算。

结果 JSON 中所有质量以**克**为单位输出为十进制字符串：能在 28 位小数内
精确表示的直接给出精确小数（本项目演示数据全部可精确表示）；除不尽的
有理数给出 28 位小数（HALF_UP）并同时附 ``*_exact`` 分数字段与全局
``all_decimals_exact`` 标志，保证复核脚本可做精确比对。占比同时给出精确
分数、十进制小数与百分数三种写法。

装载失败同样落盘为 ``INVALID_INPUT`` 结果（不产出任何谱系），修正文件后
重新装载使用新的 calc_id 即可；封存库中此前算好的其他结果不受影响、
仍可查询。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from pathlib import Path
from typing import List, Optional, Sequence

from .engine import (
    BatchRecord,
    Contribution,
    ContributionPath,
    LedgerEntry,
    LineageResult,
    LossRecord,
    RecallItem,
    RecallResult,
    build_lineage,
    query_composition,
    run_recall,
)
from .io import LineageInputError, digest_files, load_lineage_input

CALC_DIR = "lineage_calcs"

STATUS_OK = "OK"
STATUS_INVALID = "INVALID_INPUT"

# 定点展示 28 位小数；运算上下文需更宽精度以容纳整数部分与量化过程
_DECIMAL_PRECISION = 60
_QUANT_CONTEXT = Decimal(1).scaleb(-28)      # 28 位小数量化单位
_NON_TERMINATING: List[bool] = []            # 单次序列化内是否出现舍入


def _round_28(frac: Fraction) -> str:
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        value = Decimal(frac.numerator) / Decimal(frac.denominator)
        return format(value.quantize(_QUANT_CONTEXT, rounding=ROUND_HALF_UP), "f")


def _sanitize_calc_id(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text)


def _result_path(store_dir: str | Path, calc_id: str) -> Path:
    return Path(store_dir) / CALC_DIR / f"{calc_id}.json"


def _exact_decimal(frac: Fraction, max_digits: int = 28) -> Optional[str]:
    """有限十进制表示；分母在给定位数内除不尽时返回 None。"""
    denominator = frac.denominator
    # 分母只含 2/5 因子时为有限小数
    n = denominator
    for factor in (2, 5):
        while n % factor == 0:
            n //= factor
    if n != 1:
        return None
    sign = "-" if frac < 0 else ""
    text = format(abs(frac.numerator), "d")
    if denominator == 1:
        return sign + text
    # 用 Decimal 精确定点转换
    value = Decimal(frac.numerator) / Decimal(denominator)
    rendered = format(value, "f")
    if "." in rendered:
        integer, frac_part = rendered.split(".")
        if len(frac_part) > max_digits:
            return None
    return sign + rendered


def mass_text(frac: Fraction) -> str:
    """质量序列化为十进制字符串；除不尽时 28 位 HALF_UP，并登记舍入。"""
    exact = _exact_decimal(frac)
    if exact is not None:
        return exact
    _NON_TERMINATING.append(True)
    return _round_28(frac)


def fraction_exact(frac: Fraction) -> str:
    """精确有理数字符串（分子/分母；整数省略分母）。"""
    if frac.denominator == 1:
        return str(frac.numerator)
    return f"{frac.numerator}/{frac.denominator}"


def fraction_decimal(frac: Fraction) -> str:
    """占比等无量纲有理数的 28 位十进制写法（除不尽时 HALF_UP）。"""
    exact = _exact_decimal(frac)
    if exact is not None:
        return exact
    _NON_TERMINATING.append(True)
    return _round_28(frac)


def _path_dict(path: ContributionPath) -> dict:
    return {
        "batch_chain": list(path.batch_chain),
        "operation_chain": list(path.operation_chain),
        "mass_g": mass_text(path.mass_g),
        "mass_g_exact": fraction_exact(path.mass_g),
    }


def _contribution_dict(c: Contribution) -> dict:
    return {
        "raw_batch_id": c.raw_batch_id,
        "mass_g": mass_text(c.mass_g),
        "mass_g_exact": fraction_exact(c.mass_g),
        "via_operations": list(c.operation_ids),
        "paths": [_path_dict(p) for p in c.paths],
    }


def _batch_dict(rec: BatchRecord) -> dict:
    return {
        "batch_id": rec.batch_id,
        "kind": rec.kind,
        "material": rec.material,
        "producer_operation": rec.producer_operation,
        "produced_mass_g": mass_text(rec.produced_mass_g),
        "consumed_mass_g": mass_text(rec.consumed_mass_g),
        "remaining_mass_g": mass_text(rec.remaining_mass_g),
        "is_exhausted": rec.is_exhausted,
        "consumption": [
            {"operation_id": op_id, "consumed_mass_g": mass_text(m)}
            for op_id, m in rec.consumption
        ],
        "generated_composition": [_contribution_dict(c)
                                  for c in rec.generated_composition],
        "remaining_composition": [_contribution_dict(c) for c in rec.composition],
    }


def _loss_dict(rec: LossRecord) -> dict:
    return {
        "operation_id": rec.operation_id,
        "total_loss_g": mass_text(rec.total_loss_g),
        "composition": [_contribution_dict(c) for c in rec.composition],
    }


def _ledger_dict(entry: LedgerEntry) -> dict:
    return {
        "raw_batch_id": entry.raw_batch_id,
        "initial_mass_g": mass_text(entry.initial_g),
        "remaining_contribution_g": mass_text(entry.remaining_contribution_g),
        "loss_contribution_g": mass_text(entry.loss_contribution_g),
        "balanced": entry.balanced,
        "check": (f"{mass_text(entry.initial_g)} = "
                  f"{mass_text(entry.remaining_contribution_g)} + "
                  f"{mass_text(entry.loss_contribution_g)}"),
    }


def _recall_item_dict(item: RecallItem) -> dict:
    return {
        "batch_id": item.batch_id,
        "material": item.material,
        "available_mass_g": mass_text(item.remaining_mass_g),
        "suspect_mass_g": mass_text(item.suspect_mass_g),
        "suspect_share": fraction_decimal(item.suspect_share),
        "suspect_share_exact": fraction_exact(item.suspect_share),
        "suspect_share_pct": fraction_decimal(item.suspect_share * 100),
        "quarantine_mass_g": mass_text(item.remaining_mass_g),
        "suspect_raw_batches": list(item.suspect_raw_batches),
        "evidence": [_contribution_dict(c) for c in item.evidence],
    }


def _recall_dict(recall: RecallResult) -> dict:
    return {
        "suspect_raw_batches": list(recall.suspect_raw_batches),
        "total_quarantine_mass_g": mass_text(recall.total_quarantine_g),
        "items": [_recall_item_dict(it) for it in recall.items],
        "suspect_ledgers": [_ledger_dict(e) for e in recall.ledgers],
    }


def _serialize(result: LineageResult, base: dict, calc_id: str) -> dict:
    _NON_TERMINATING.clear()
    payload = {
        **base,
        "calc_id": calc_id,
        "status": STATUS_OK,
        "mass_unit": "g",
        "arithmetic": {
            "type": "exact rational (fractions.Fraction)",
            "decimal_digits": 28,
            "rounding": "ROUND_HALF_UP（仅用于除不尽的有理数展示）",
            "all_decimals_exact": True,  # 序列化结束后按实际情况改写
        },
        "assumptions": list(result.assumptions),
        "raw_batches": [
            {
                "batch_id": rb.batch_id,
                "material": rb.material,
                "initial_mass_g": mass_text(rb.initial.grams),
                "initial_as_written": {
                    "mass": rb.initial.raw_text,
                    "unit": rb.initial.unit,
                },
            } for rb in result.raw_batches
        ],
        "operations": [
            {
                "operation_id": op.operation_id,
                "inputs": [
                    {"batch_id": line.batch_id,
                     "mass_g": mass_text(line.qty.grams),
                     "as_written": {"mass": line.qty.raw_text, "unit": line.qty.unit}}
                    for line in op.inputs
                ],
                "outputs": [
                    {"batch_id": line.batch_id,
                     "mass_g": mass_text(line.qty.grams),
                     "as_written": {"mass": line.qty.raw_text, "unit": line.qty.unit}}
                    for line in op.outputs
                ],
                "loss_mass_g": mass_text(op.loss.grams),
                "loss_as_written": {"mass": op.loss.raw_text, "unit": op.loss.unit},
            } for op in result.operations
        ],
        "batches": [_batch_dict(b) for b in result.batches],
        "losses": [_loss_dict(l) for l in result.losses],
        "global_ledger": [_ledger_dict(e) for e in result.ledger],
    }
    payload["arithmetic"]["all_decimals_exact"] = not bool(_NON_TERMINATING)
    _NON_TERMINATING.clear()
    return payload


def _save(store_dir: str | Path, calc_id: str, payload: dict) -> None:
    path = _result_path(store_dir, calc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def load_lineage(materials: str | Path, operations: str | Path,
                 store_dir: str | Path,
                 calc_id: Optional[str] = None) -> dict:
    """读取两份记录、校验、计算谱系并落盘；失败也产出可读的 INVALID_INPUT。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    files = [Path(materials), Path(operations)]
    base = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input": {
            "materials": str(materials),
            "operations": str(operations),
        },
    }
    try:
        digest = digest_files(files)
    except OSError as exc:
        calc_id = _sanitize_calc_id(calc_id or f"invalid-{stamp}")
        payload = {**base, "calc_id": calc_id, "status": STATUS_INVALID,
                   "errors": [f"输入文件不可读: {exc}"],
                   "note": "输入不合法，本次装载被拒绝；未产出任何谱系。"}
        _save(store_dir, calc_id, payload)
        return payload
    base["input"]["sha256"] = digest

    try:
        raw_batches, operations_loaded = load_lineage_input(materials, operations)
    except LineageInputError as exc:
        calc_id = _sanitize_calc_id(calc_id or f"invalid-{stamp}")
        payload = {
            **base,
            "calc_id": calc_id,
            "status": STATUS_INVALID,
            "errors": exc.problems,
            "note": "输入不合法，本次装载被整体拒绝；未跳过任何坏操作，未产出谱系。",
        }
        _save(store_dir, calc_id, payload)
        return payload

    result = build_lineage(raw_batches, operations_loaded)
    calc_id = _sanitize_calc_id(calc_id or f"lineage-{stamp}")
    payload = _serialize(result, base, calc_id)
    _save(store_dir, calc_id, payload)
    return payload


def load_saved(store_dir: str | Path, calc_id: str) -> dict:
    path = _result_path(store_dir, calc_id)
    if not path.exists():
        known = sorted(p.stem for p in path.parent.glob("*.json")) \
            if path.parent.exists() else []
        raise KeyError(f"未找到谱系计算结果 {calc_id!r}（已有: {known}）")
    return json.loads(path.read_text(encoding="utf-8"))


def get_result(store_dir: str | Path, calc_id: str) -> LineageResult:
    """读取已保存结果并还原为内存中的精确 LineageResult（用于查询/召回）。"""
    payload = load_saved(store_dir, calc_id)
    if payload.get("status") != STATUS_OK:
        raise ValueError(f"结果 {calc_id} 状态为 {payload.get('status')}，"
                         f"不含可查询谱系")
    raw_batches, operations = load_lineage_input(
        _input_path(payload, "materials"), _input_path(payload, "operations"))
    return build_lineage(raw_batches, operations)


def _input_path(payload: dict, key: str) -> str:
    return payload["input"][key]


def show_composition(store_dir: str | Path, calc_id: str, batch_id: str) -> dict:
    result = get_result(store_dir, calc_id)
    record = query_composition(result, batch_id)
    _NON_TERMINATING.clear()
    out = {
        "calc_id": calc_id,
        "batch_id": record.batch_id,
        "kind": record.kind,
        "material": record.material,
        "producer_operation": record.producer_operation,
        "generated_mass_g": mass_text(record.produced_mass_g),
        "consumed_mass_g": mass_text(record.consumed_mass_g),
        "remaining_mass_g": mass_text(record.remaining_mass_g),
        "is_exhausted": record.is_exhausted,
        "consumption": [
            {"operation_id": op_id, "consumed_mass_g": mass_text(m)}
            for op_id, m in record.consumption
        ],
        "generated_composition": [_contribution_dict(c)
                                  for c in record.generated_composition],
        "remaining_composition": [_contribution_dict(c) for c in record.composition],
        "assumptions": list(result.assumptions),
    }
    out["all_decimals_exact"] = not bool(_NON_TERMINATING)
    _NON_TERMINATING.clear()
    return out


def recall(store_dir: str | Path, calc_id: str,
           suspect_raw_batches: Sequence[str]) -> dict:
    result = get_result(store_dir, calc_id)
    recall_result = run_recall(result, suspect_raw_batches)
    _NON_TERMINATING.clear()
    out = {"calc_id": calc_id, **_recall_dict(recall_result),
           "assumptions": list(result.assumptions)}
    out["all_decimals_exact"] = not bool(_NON_TERMINATING)
    _NON_TERMINATING.clear()
    return out
