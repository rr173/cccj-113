"""配料计算输入的读取与校验。

输入四类文件（全部为质量/数量数据，与 MES 活动轨迹无关）：

- ``inventory.csv``   库存表：lot_id, material, mode, available_kg, price_per_kg, drum_net_kg
- ``certificates.csv``证书登记表：cert_id, lab, issued_at
- ``assays.csv``      化验表：lot_id, element, cert_id, unit, value_type, value, lower, upper
- ``heat.json``       炉次要求：总投料质量、各元素上下限、互斥原料对

校验原则：缺证书、同批同元素相互冲突的有效结果、负数、单位不明、
受约束元素缺化验数据等一律拒绝本次计算（抛 :class:`ChargeInputError`），
不把缺值填成零。所有问题一次性收集后抛出，便于一次性修齐。
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .models import (
    FEED_MODES,
    MODE_BULK,
    MODE_DRUM,
    UNITS,
    UNIT_PCT,
    UNIT_PPM,
    VALUE_LIMIT,
    VALUE_RANGE,
    VALUE_SINGLE,
    AssayInterval,
    ChargeInput,
    HeatElement,
    HeatSpec,
    Lot,
    to_fraction,
)


class ChargeInputError(ValueError):
    """输入数据不合法：携带全部问题描述，拒绝本次计算。"""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("输入数据不合法，拒绝计算:\n" + "\n".join(f"- {p}" for p in self.problems))


def digest_files(paths: Sequence[str | Path]) -> str:
    """输入文件字节级 SHA-256（按文件名排序拼接），用于结果可追溯。"""
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _parse_float(raw: str, *, where: str, problems: List[str],
                 allow_empty: bool = False) -> Optional[float]:
    text = (raw or "").strip()
    if not text:
        if allow_empty:
            return None
        problems.append(f"{where}: 数值缺失")
        return None
    try:
        value = float(text)
    except ValueError:
        problems.append(f"{where}: 无法解析为数值: {text!r}")
        return None
    if not math.isfinite(value):
        problems.append(f"{where}: 数值必须有限: {text!r}")
        return None
    return value


def _read_csv(path: str | Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def load_inventory(path: str | Path, problems: List[str]) -> Tuple[Lot, ...]:
    """读取库存表；问题写入 ``problems`` 而不是立即抛出。"""
    lots: List[Lot] = []
    seen: set[str] = set()
    for i, row in enumerate(_read_csv(path), start=2):  # 第 1 行是表头
        where = f"inventory.csv 第{i}行"
        lot_id = (row.get("lot_id") or "").strip()
        if not lot_id:
            problems.append(f"{where}: lot_id 为空")
            continue
        if lot_id in seen:
            problems.append(f"{where}: lot_id 重复: {lot_id}")
            continue
        seen.add(lot_id)

        material = (row.get("material") or "").strip() or lot_id
        mode = (row.get("mode") or "").strip().lower()
        if mode not in FEED_MODES:
            problems.append(f"{where}: 投料方式不明: {mode!r}（应为 bulk/drum）")
            continue

        available = _parse_float(row.get("available_kg", ""), where=f"{where} available_kg",
                                 problems=problems)
        price = _parse_float(row.get("price_per_kg", ""), where=f"{where} price_per_kg",
                             problems=problems)
        net_raw = (row.get("drum_net_kg") or "").strip()
        net = _parse_float(net_raw, where=f"{where} drum_net_kg", problems=problems,
                           allow_empty=True)
        if available is not None and available < 0:
            problems.append(f"{where}: available_kg 为负数: {available}")
        if price is not None and price < 0:
            problems.append(f"{where}: price_per_kg 为负数: {price}")
        if mode == MODE_DRUM:
            if net is None:
                problems.append(f"{where}: 整桶原料必须给出 drum_net_kg")
            elif net <= 0:
                problems.append(f"{where}: drum_net_kg 必须为正: {net}")
        if mode == MODE_BULK and net is not None:
            problems.append(f"{where}: 散装原料不应填写 drum_net_kg")
        if available is None or price is None:
            continue
        if mode == MODE_DRUM and net is None:
            continue
        lots.append(Lot(lot_id=lot_id, material=material, mode=mode,
                        available_kg=available, price_per_kg=price,
                        drum_net_kg=net if mode == MODE_DRUM else None))
    return tuple(lots)


def load_certificates(path: str | Path, problems: List[str]) -> frozenset[str]:
    """读取证书登记表，返回有效证书号集合。"""
    certs: set[str] = set()
    for i, row in enumerate(_read_csv(path), start=2):
        where = f"certificates.csv 第{i}行"
        cert_id = (row.get("cert_id") or "").strip()
        if not cert_id:
            problems.append(f"{where}: cert_id 为空")
            continue
        if cert_id in certs:
            problems.append(f"{where}: cert_id 重复: {cert_id}")
            continue
        certs.add(cert_id)
    return frozenset(certs)


def load_assays(path: str | Path, valid_certs: frozenset[str],
                problems: List[str]) -> Tuple[AssayInterval, ...]:
    """读取化验表并合并同批同元素的多条有效结果（区间取交）。

    - ``single``：``value``，区间 [v, v]；
    - ``range`` ：``lower``/``upper``，区间 [lower, upper]；
    - ``limit`` ：``upper`` 为检出限，闭区间 [0, upper]，**不取中点**。

    同一 (lot_id, element) 的多条有效结果取区间交；交集为空即相互冲突，拒绝计算。
    """
    # (lot_id, element) -> [lo, hi, cert_ids]
    merged: Dict[Tuple[str, str], List[object]] = {}
    for i, row in enumerate(_read_csv(path), start=2):
        where = f"assays.csv 第{i}行"
        lot_id = (row.get("lot_id") or "").strip()
        element = (row.get("element") or "").strip()
        cert_id = (row.get("cert_id") or "").strip()
        unit = (row.get("unit") or "").strip().lower()
        value_type = (row.get("value_type") or "").strip().lower()
        if not lot_id or not element:
            problems.append(f"{where}: lot_id/element 为空")
            continue
        if not cert_id:
            problems.append(f"{where}: {lot_id}/{element} 缺证书号")
            continue
        if cert_id not in valid_certs:
            problems.append(f"{where}: {lot_id}/{element} 引用的证书 {cert_id} 未登记")
            continue
        if unit not in UNITS:
            problems.append(f"{where}: {lot_id}/{element} 单位不明: {unit!r}（应为 pct/ppm）")
            continue

        lo_raw = hi_raw = None
        if value_type == VALUE_SINGLE:
            lo_raw = hi_raw = _parse_float(row.get("value", ""),
                                           where=f"{where} value", problems=problems)
        elif value_type == VALUE_RANGE:
            lo_raw = _parse_float(row.get("lower", ""), where=f"{where} lower",
                                  problems=problems)
            hi_raw = _parse_float(row.get("upper", ""), where=f"{where} upper",
                                  problems=problems)
        elif value_type == VALUE_LIMIT:
            lo_raw = 0.0
            hi_raw = _parse_float(row.get("upper", ""), where=f"{where} upper(检出限)",
                                  problems=problems)
        else:
            problems.append(f"{where}: {lot_id}/{element} 值类型不明: {value_type!r}"
                            f"（应为 single/range/limit）")
            continue
        if lo_raw is None or hi_raw is None:
            continue
        if lo_raw < 0 or hi_raw < 0:
            problems.append(f"{where}: {lot_id}/{element} 化验值出现负数: "
                            f"[{lo_raw}, {hi_raw}] {unit}")
            continue
        if lo_raw > hi_raw:
            problems.append(f"{where}: {lot_id}/{element} 下界大于上界: "
                            f"[{lo_raw}, {hi_raw}] {unit}")
            continue
        lo = to_fraction(lo_raw, unit)
        hi = to_fraction(hi_raw, unit)

        key = (lot_id, element)
        if key not in merged:
            merged[key] = [lo, hi, [cert_id]]
        else:
            slot = merged[key]
            slot[0] = max(slot[0], lo)  # type: ignore[index]
            slot[1] = min(slot[1], hi)  # type: ignore[index]
            slot[2].append(cert_id)     # type: ignore[union-attr]
            if slot[0] > slot[1]:       # type: ignore[operator]
                problems.append(
                    f"{lot_id}/{element}: 有效化验结果相互冲突，区间无交集"
                    f"（涉及证书 {sorted(slot[2])}），拒绝计算"
                )

    return tuple(
        AssayInterval(lot_id=k[0], element=k[1], lo=v[0], hi=v[1],  # type: ignore[index]
                      cert_ids=tuple(sorted(v[2])))                 # type: ignore[index]
        for k, v in sorted(merged.items())
    )


def load_heat(path: str | Path, problems: List[str]) -> Optional[HeatSpec]:
    """读取炉次要求 JSON。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"heat.json: 无法解析: {exc}")
        return None
    if not isinstance(raw, dict):
        problems.append("heat.json: 顶层必须是对象")
        return None

    heat_id = str(raw.get("heat_id") or "").strip()
    if not heat_id:
        problems.append("heat.json: heat_id 缺失")
        heat_id = "UNKNOWN"
    mass = raw.get("total_mass_kg")
    if not isinstance(mass, (int, float)) or not math.isfinite(float(mass)):
        problems.append("heat.json: total_mass_kg 缺失或不是数值")
        mass = None
    elif float(mass) <= 0:
        problems.append(f"heat.json: total_mass_kg 必须为正: {mass}")
        mass = None

    elements: List[HeatElement] = []
    raw_elements = raw.get("elements", {})
    if not isinstance(raw_elements, dict):
        problems.append("heat.json: elements 必须是对象")
        raw_elements = {}
    suffix_unit = {"min_pct": (UNIT_PCT, "min"), "max_pct": (UNIT_PCT, "max"),
                   "min_ppm": (UNIT_PPM, "min"), "max_ppm": (UNIT_PPM, "max")}
    for element, spec in sorted(raw_elements.items()):
        if not isinstance(spec, dict):
            problems.append(f"heat.json: 元素 {element} 的要求必须是对象")
            continue
        unknown = set(spec) - set(suffix_unit)
        if unknown:
            problems.append(f"heat.json: 元素 {element} 含未知字段 {sorted(unknown)}"
                            f"（允许 min_pct/max_pct/min_ppm/max_ppm）")
            continue
        units_used = {suffix_unit[k][0] for k in spec}
        if len(units_used) > 1:
            problems.append(f"heat.json: 元素 {element} 的上下限混用了不同单位")
            continue
        req_min = req_max = None
        unit = next(iter(units_used)) if units_used else UNIT_PCT
        for key, (u, side) in suffix_unit.items():
            if key not in spec:
                continue
            value = spec[key]
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                problems.append(f"heat.json: 元素 {element} 的 {key} 不是数值")
                continue
            if float(value) < 0:
                problems.append(f"heat.json: 元素 {element} 的 {key} 为负数: {value}")
                continue
            if side == "min":
                req_min = to_fraction(float(value), u)
            else:
                req_max = to_fraction(float(value), u)
        if req_min is None and req_max is None:
            problems.append(f"heat.json: 元素 {element} 未给出任何上下限")
            continue
        if req_min is not None and req_max is not None and req_min > req_max:
            problems.append(f"heat.json: 元素 {element} 下限大于上限")
            continue
        elements.append(HeatElement(element=str(element), req_min=req_min,
                                    req_max=req_max, unit=unit))

    mutex_pairs: List[Tuple[str, str]] = []
    raw_mutex = raw.get("mutex", [])
    if not isinstance(raw_mutex, list):
        problems.append("heat.json: mutex 必须是数组")
        raw_mutex = []
    for pair in raw_mutex:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            problems.append(f"heat.json: 互斥对必须是二元组: {pair!r}")
            continue
        a, b = str(pair[0]).strip(), str(pair[1]).strip()
        if a == b:
            problems.append(f"heat.json: 互斥对两端相同: {a}")
            continue
        mutex_pairs.append((a, b))

    if mass is None:
        return None
    return HeatSpec(heat_id=heat_id, total_mass_kg=float(mass),
                    elements=tuple(elements), mutex_pairs=tuple(mutex_pairs))


def load_charge_input(inventory: str | Path, assays: str | Path,
                      certificates: str | Path, heat: str | Path) -> ChargeInput:
    """读取并交叉校验全部输入；任何数据问题都使本次计算被拒绝。"""
    problems: List[str] = []
    certs = load_certificates(certificates, problems)
    lots = load_inventory(inventory, problems)
    intervals = load_assays(assays, certs, problems)
    spec = load_heat(heat, problems)

    lot_ids = {lot.lot_id for lot in lots}
    assay_lot_ids = {a.lot_id for a in intervals}
    for unknown in sorted(assay_lot_ids - lot_ids):
        problems.append(f"assays.csv: 批次 {unknown} 不在库存表中")

    if spec is not None:
        constrained = {e.element for e in spec.elements}
        have = {(a.lot_id, a.element) for a in intervals}
        for lot in lots:
            for element in sorted(constrained):
                if (lot.lot_id, element) not in have:
                    problems.append(
                        f"批次 {lot.lot_id} 缺受约束元素 {element} 的化验数据，"
                        f"不按零填充，拒绝计算"
                    )
        for a, b in spec.mutex_pairs:
            for lot_id in (a, b):
                if lot_id not in lot_ids:
                    problems.append(f"heat.json: 互斥对引用的批次 {lot_id} 不在库存表中")
        # 互斥对去重（顺序无关）
        seen_pairs = set()
        for a, b in spec.mutex_pairs:
            key = tuple(sorted((a, b)))
            if key in seen_pairs:
                problems.append(f"heat.json: 互斥对重复: {a}/{b}")
            seen_pairs.add(key)

    if problems:
        raise ChargeInputError(problems)
    assert spec is not None
    # 只保留受约束元素的化验区间，其余元素不参与求解
    constrained = {e.element for e in spec.elements}
    kept = tuple(a for a in intervals if a.element in constrained)
    return ChargeInput(lots=lots, assays=kept, heat=spec)
