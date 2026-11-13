"""交叉型量具 R&R（Gage R&R crossed）的不可变值对象。

约定：
- 长度量纲支持 mm 与 μm（接受 um/µm 别名），内部统一换算到试验声明的
  量纲（canonical unit），结果一律以该量纲报告；
- 观测行保留原始读数与原始单位，另存换算后的 canonical 值，两者都可追溯；
- 规格上下限以试验量纲书写，只在两者同时给出时有效。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

UNIT_MM = "mm"
UNIT_UM = "μm"
LENGTH_UNITS = (UNIT_MM, UNIT_UM)

_UNIT_ALIASES = {
    "mm": UNIT_MM,
    "um": UNIT_UM,
    "μm": UNIT_UM,  # U+03BC 希腊字母 mu
    "µm": UNIT_UM,  # U+00B5 微符号
}

_UM_PER_MM = 1000.0


def normalize_unit(text: str) -> Optional[str]:
    """规范化单位书写；无法识别时返回 None（由调用方记为输入问题）。"""
    key = (text or "").strip().lower()
    return _UNIT_ALIASES.get(key)


def convert_length(value: float, from_unit: str, to_unit: str) -> float:
    """mm/μm 之间换算；同单位原样返回，其余组合抛 ValueError。"""
    if from_unit == to_unit:
        return value
    if from_unit == UNIT_MM and to_unit == UNIT_UM:
        return value * _UM_PER_MM
    if from_unit == UNIT_UM and to_unit == UNIT_MM:
        return value / _UM_PER_MM
    raise ValueError(f"不兼容的长度单位换算: {from_unit!r} -> {to_unit!r}")


@dataclass(frozen=True)
class StudySpec:
    """一次量具 R&R 试验的定义（只支持完整平衡交叉设计）。"""

    study_id: str
    characteristic: str
    unit: str  # canonical 量纲（mm / μm）
    parts: Tuple[str, ...]
    operators: Tuple[str, ...]
    repeats: int  # 每格声明的重复次数（设计校验要求 >= 2）
    spec_lower: Optional[float] = None
    spec_upper: Optional[float] = None

    @property
    def spec_width(self) -> Optional[float]:
        if self.spec_lower is None or self.spec_upper is None:
            return None
        return self.spec_upper - self.spec_lower


@dataclass(frozen=True)
class Observation:
    """一行测量观测（已通过输入校验并换算到试验量纲）。"""

    row: int  # observations.csv 物理行号（表头为第 1 行）
    study_id: str
    part_id: str
    operator_id: str
    repeat_id: int
    value: float  # 原始读数
    unit: str  # 原始单位（规范化后）
    canonical_value: float  # 换算到试验量纲后的读数


@dataclass(frozen=True)
class GageInput:
    """通过全部校验的一次试验输入。"""

    study: StudySpec
    observations: Tuple[Observation, ...]
