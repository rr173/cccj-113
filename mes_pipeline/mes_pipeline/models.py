"""管线内部数据模型（不可变值对象）。

所有时间在解析阶段统一转为 UTC aware datetime；原始 wall_clock 字符串保留在记录中。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import FrozenSet, Optional, Tuple


@dataclass(frozen=True)
class RawEvent:
    """一条 MES 导出事件行（原始、不可变）。"""

    source_event_id: str
    line_code: str
    batch_label: str
    operation: str
    action: str  # OPEN / CLOSE
    wall_clock: str  # 原始字符串，原样保留
    row_number: int  # 导出行号（从 1 开始）
    package_id: str
    handover_barcode: Optional[str] = None  # 交接条码（可缺失）


@dataclass(frozen=True)
class Withdrawal:
    """撤回单：按 source_event_id 逻辑撤回一条事件，撤回单号保留。"""

    withdrawal_id: str
    source_event_id: str
    package_id: str
    reason: str = ""


@dataclass(frozen=True)
class BarcodeCorrection:
    """交接条码修正：把某产线批次的条码由旧值改为新值（可为 None 表示清除）。

    修正可能引发合成批次拆分，因此按关联批次整体重建分析。
    """

    correction_id: str
    line_code: str
    batch_label: str
    old_barcode: Optional[str]
    new_barcode: Optional[str]
    package_id: str


@dataclass(frozen=True)
class CalibratedEvent:
    """校准后的事件：UTC 时刻 + 稳定排序键。"""

    raw: RawEvent
    utc_time: datetime

    @property
    def sort_key(self) -> Tuple[datetime, int, str]:
        # 同刻事件按导出行号稳定排序；行号也相同则用 source_event_id 兜底
        return (self.utc_time, self.raw.row_number, self.raw.source_event_id)


@dataclass(frozen=True)
class LineBatch:
    """产线内批次：batch_label 仅在 line_code 内唯一。"""

    line_code: str
    batch_label: str

    @property
    def key(self) -> Tuple[str, str]:
        return (self.line_code, self.batch_label)


@dataclass(frozen=True)
class Violation:
    """一条工艺约束违规。

    violation_type:
      - MISSING_PREDECESSOR: OPEN 时规定的前序操作尚未完成（阻断边 blocking_edge）
      - MISSING_REQUIRED     : 轨迹结束时必需工序从未出现（blocking_edge 为 None）
      - CLOSE_WITHOUT_OPEN   : CLOSE 没有可配对的 OPEN
      - UNKNOWN_ACTION       : action 不是 OPEN/CLOSE
    """

    synthetic_batch_id: str
    violation_type: str
    blocking_edge: Optional[Tuple[str, str]]
    operation: str
    action: str
    source_event_id: Optional[str]
    line_code: Optional[str]
    row_number: Optional[int]
    package_id: Optional[str]
    detail: str

    def to_dict(self) -> dict:
        return {
            "synthetic_batch_id": self.synthetic_batch_id,
            "violation_type": self.violation_type,
            "blocking_edge": list(self.blocking_edge) if self.blocking_edge else None,
            "operation": self.operation,
            "action": self.action,
            "source_event_id": self.source_event_id,
            "line_code": self.line_code,
            "row_number": self.row_number,
            "package_id": self.package_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class WaitSample:
    """单个 operation 的一次等待样本（秒）。

    等待时长 = 本操作 OPEN 时刻 - 轨迹中上一操作 CLOSE 时刻。
    轨迹第一个操作没有前驱，不产生样本。
    """

    synthetic_batch_id: str
    line_code: str
    operation: str
    wait_seconds: float
    open_event_id: str
    prior_close_event_id: str


@dataclass(frozen=True)
class SyntheticBatch:
    """一个合成批次：可能由多条产线的 LineBatch 经交接条码关联而成。"""

    synthetic_batch_id: str
    members: FrozenSet[LineBatch]
    events: Tuple[CalibratedEvent, ...]  # 已按 sort_key 排序
    trace: Tuple[str, ...]  # 有序活动名，格式 "OPERATION:ACTION"
    unassigned: bool = False
    unassigned_reason: str = ""
    dangling: Tuple[Tuple[RawEvent, str], ...] = field(default_factory=tuple)
    violations: Tuple[Violation, ...] = field(default_factory=tuple)
    wait_samples: Tuple[WaitSample, ...] = field(default_factory=tuple)
    rework_count: int = 0
