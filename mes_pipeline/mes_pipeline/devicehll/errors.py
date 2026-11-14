"""devicehll 子系统的错误类型。命令行按类型映射退出码。"""
from __future__ import annotations

from typing import List, Optional


class DeviceHllError(Exception):
    """devicehll 子系统全部受检错误的基类。"""


class SketchIOError(DeviceHllError):
    """逐行输入不合法（坏行）：消息含文件与行号。"""

    def __init__(self, problems: Sequence[str]):
        self.problems: List[str] = list(problems)
        super().__init__("输入不合法，拒绝处理:\n" + "\n".join(f"- {p}" for p in self.problems))


class SketchFormatError(DeviceHllError):
    """摘要文件格式错误：截断/校验和/未知版本/非法状态等。"""


class SketchCompatibilityError(DeviceHllError):
    """精度、seed、哈希版本或模式不兼容：在产出结果之前拒绝。"""


class SketchEstimationError(DeviceHllError):
    """估计无法定义（如 64 位大范围修正超出定义域）：明确失败。"""
