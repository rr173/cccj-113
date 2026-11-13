"""实耗批次谱系的不可变值对象。

内部质量一律使用 :class:`~fractions.Fraction` 精确表示，单位统一为克（g）；
输入允许 kg 与 g 混写（1 kg = 1000 g），输出同时保留原始书写单位与换算值。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Tuple

UNIT_G = "g"
UNIT_KG = "kg"
MASS_UNITS = (UNIT_G, UNIT_KG)

_G_PER_KG = 1000


@dataclass(frozen=True)
class MassQty:
    """一个带单位的质量值；内部以克为单位做精确有理数存储。"""

    grams: Fraction
    unit: str           # 原始书写单位（规范化后的 g / kg）
    raw_text: str       # 原始十进制文本（用于结果回显与问题定位）

    @property
    def in_unit(self) -> Fraction:
        """按原始书写单位表示的精确质量。"""
        return self.grams if self.unit == UNIT_G else self.grams / _G_PER_KG

    def to_unit(self, unit: str) -> Fraction:
        if unit == UNIT_G:
            return self.grams
        if unit == UNIT_KG:
            return self.grams / _G_PER_KG
        raise ValueError(f"不支持的质量单位: {unit!r}")


@dataclass(frozen=True)
class RawBatch:
    """外购原料批声明：批次 ID、物料名、初始质量。"""

    batch_id: str
    material: str
    initial: MassQty
    row: int            # 来源文件中的记录序号（第 1 条数据记录为 1）


@dataclass(frozen=True)
class MassLine:
    """操作内的一行物料流向（输入实耗或产出），携带来源定位。"""

    batch_id: str
    qty: MassQty
    row: int            # 来源文件中的记录序号（第 1 条数据记录为 1）


@dataclass(frozen=True)
class Operation:
    """一项加工操作：唯一 operation_id、输入行、产出行、损耗质量。"""

    operation_id: str
    inputs: Tuple[MassLine, ...]
    outputs: Tuple[MassLine, ...]
    loss: MassQty       # 允许为零（raw_text 仍记录原始书写）
    row: int            # 操作记录在操作文件中的序号（第 1 条为 1）

    @property
    def input_total_g(self) -> Fraction:
        total = Fraction(0)
        for line in self.inputs:
            total += line.qty.grams
        return total

    @property
    def output_total_g(self) -> Fraction:
        total = Fraction(0)
        for line in self.outputs:
            total += line.qty.grams
        return total
