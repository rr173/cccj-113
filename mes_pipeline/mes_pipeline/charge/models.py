"""配料计算的不可变值对象。

约定：
- 元素含量在内部统一为**质量分数**（1% = 1e-2，1 ppm = 1e-6）；
- 化验值一律保存为闭区间 ``[lo, hi]``：单值是 ``lo == hi`` 的特例，
  检出限 ``<x`` 是 ``[0, x]``，**不取中点**；
- 投料方式 ``bulk`` 按连续重量取用，``drum`` 必须按整桶（净重整数倍）投料。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# 投料方式
MODE_BULK = "bulk"
MODE_DRUM = "drum"
FEED_MODES = (MODE_BULK, MODE_DRUM)

# 化验值类型
VALUE_SINGLE = "single"  # 单值
VALUE_RANGE = "range"    # 上下界
VALUE_LIMIT = "limit"    # 带检出限的 "<" 值，区间 [0, limit]

# 含量单位
UNIT_PCT = "pct"
UNIT_PPM = "ppm"
UNITS = (UNIT_PCT, UNIT_PPM)


def to_fraction(value: float, unit: str) -> float:
    """把 pct/ppm 含量换算成质量分数。"""
    if unit == UNIT_PCT:
        return value / 100.0
    if unit == UNIT_PPM:
        return value / 1.0e6
    raise ValueError(f"未知含量单位: {unit!r}")


def from_fraction(fraction: float, unit: str) -> float:
    """把质量分数换算回指定单位（用于结果展示）。"""
    if unit == UNIT_PCT:
        return fraction * 100.0
    if unit == UNIT_PPM:
        return fraction * 1.0e6
    raise ValueError(f"未知含量单位: {unit!r}")


@dataclass(frozen=True)
class Lot:
    """一批库存原料。

    ``drum_net_kg`` 仅 ``mode == drum`` 时有效；整桶批的可用上限为
    ``floor(available_kg / drum_net_kg) * drum_net_kg``，余下不足一桶的部分不可用。
    """

    lot_id: str
    material: str
    mode: str  # bulk / drum
    available_kg: float
    price_per_kg: float
    drum_net_kg: Optional[float] = None

    @property
    def usable_kg(self) -> float:
        """实际可投料上限（整桶批向下取整到整桶）。"""
        if self.mode == MODE_DRUM:
            assert self.drum_net_kg is not None
            full_drums = math.floor(self.available_kg / self.drum_net_kg + 1e-9)
            return full_drums * self.drum_net_kg
        return self.available_kg

    @property
    def unusable_remainder_kg(self) -> float:
        """不足一桶而不可用的库存余量（散装批恒为 0）。"""
        return self.available_kg - self.usable_kg


@dataclass(frozen=True)
class AssayInterval:
    """某批次某元素合并后的化验区间（质量分数闭区间）。"""

    lot_id: str
    element: str
    lo: float
    hi: float
    cert_ids: Tuple[str, ...]  # 合并该区间的全部有效证书号


@dataclass(frozen=True)
class HeatElement:
    """炉次对单个元素的要求（质量分数；缺省侧为 None）。"""

    element: str
    req_min: Optional[float] = None
    req_max: Optional[float] = None
    unit: str = UNIT_PCT  # 炉次要求书写时使用的单位，结果展示沿用


@dataclass(frozen=True)
class HeatSpec:
    """目标炉次要求。"""

    heat_id: str
    total_mass_kg: float
    elements: Tuple[HeatElement, ...]
    mutex_pairs: Tuple[Tuple[str, str], ...] = ()  # 不能同时入炉的 lot_id 对


@dataclass(frozen=True)
class ChargeInput:
    """一次配料计算的全部输入（已通过校验）。"""

    lots: Tuple[Lot, ...]
    assays: Tuple[AssayInterval, ...]  # 每个 (lot_id, 受约束元素) 恰好一条
    heat: HeatSpec

    def assay_map(self) -> Dict[Tuple[str, str], AssayInterval]:
        return {(a.lot_id, a.element): a for a in self.assays}


@dataclass(frozen=True)
class PlanLine:
    """单批原料的投料结果。"""

    lot_id: str
    material: str
    mode: str
    kg: float
    drums: Optional[int]  # 整桶批为桶数，散装批为 None
    price_per_kg: float
    cost: float


@dataclass(frozen=True)
class ElementCheck:
    """单个元素的最坏口径核算（均已换算回炉次要求的书写单位）。"""

    element: str
    unit: str
    worst_min: Optional[float]  # 各原料下界核算的最低含量（无 min 要求时为 None）
    worst_max: Optional[float]  # 各原料上界核算的最高含量（无 max 要求时为 None）
    req_min: Optional[float]
    req_max: Optional[float]
    margin_min: Optional[float]  # worst_min - req_min，>= 0 为满足
    margin_max: Optional[float]  # req_max - worst_max，>= 0 为满足


# 求解状态
STATUS_OPTIMAL = "OPTIMAL"
STATUS_INFEASIBLE = "INFEASIBLE"
STATUS_INVALID = "INVALID_INPUT"
STATUS_NODE_LIMIT = "NODE_LIMIT_EXCEEDED"
