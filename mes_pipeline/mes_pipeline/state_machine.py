"""独立小型状态机：逐行解释单个合成批次的轨迹。

不依赖 pandas / pm4py，仅消费本项目的值对象，便于人工核对：
每读入一行事件就做一次状态转移，并记录转移结果与原始行位置。

状态语义
--------
- open_stacks[operation]: 已 OPEN 尚未 CLOSE 的事件栈（后进先出配对，支持返工重入）；
- completed[operation]: 该操作被 CLOSE 完成的次数（>0 即视为前序已完成）；
- last_close: 轨迹中上一条 CLOSE 事件，用于计算下一次 OPEN 的等待时长；
- seen[operation]: 出现过的操作集合，用于轨迹结束时判定缺失必需工序。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .config import ProcessSpec
from .linking import LinkedGroup
from .models import (
    CalibratedEvent,
    RawEvent,
    SyntheticBatch,
    Violation,
    WaitSample,
)

VALID_ACTIONS = frozenset({"OPEN", "CLOSE"})


@dataclass
class _Transition:
    """一次状态转移的可读解释（测试与调试用）。"""

    step: int
    source_event_id: str
    line_code: str
    row_number: int
    operation: str
    action: str
    accepted: bool
    note: str


class BatchStateMachine:
    """逐行解释器。一个实例处理一个批次。"""

    def __init__(self, spec: ProcessSpec, synthetic_batch_id: str):
        self.spec = spec
        self.batch_id = synthetic_batch_id
        self.open_stacks: Dict[str, List[CalibratedEvent]] = {}
        self.completed: Dict[str, int] = {}
        self.seen: set[str] = set()
        self.last_close: Optional[CalibratedEvent] = None
        self.rework_count = 0
        self.transitions: List[_Transition] = []

    # ------------------------------------------------------------------ #
    def _violation(
        self,
        vtype: str,
        event: CalibratedEvent,
        detail: str,
        blocking_edge: Optional[Tuple[str, str]] = None,
    ) -> Violation:
        raw = event.raw
        return Violation(
            synthetic_batch_id=self.batch_id,
            violation_type=vtype,
            blocking_edge=blocking_edge,
            operation=raw.operation,
            action=raw.action,
            source_event_id=raw.source_event_id,
            line_code=raw.line_code,
            row_number=raw.row_number,
            package_id=raw.package_id,
            detail=detail,
        )

    def _check_predecessors(self, event: CalibratedEvent) -> List[Violation]:
        """OPEN 前检查普通前序边：所有非可选前序必须已完成至少一次。

        返工边不参与阻断（它表达的就是“前序完成后允许再次进入”）。
        """
        op = event.raw.operation
        violations: List[Violation] = []
        for pred in sorted(self.spec.predecessors(op)):
            if pred in self.spec.optional and self.completed.get(pred, 0) == 0:
                continue  # 可选前序未做不阻断
            if self.completed.get(pred, 0) == 0:
                violations.append(
                    self._violation(
                        "MISSING_PREDECESSOR",
                        event,
                        f"操作 {op} 在 {pred} 完成前出现，违反前序边 {pred}->{op}",
                        blocking_edge=(pred, op),
                    )
                )
        return violations

    # ------------------------------------------------------------------ #
    def step(self, event: CalibratedEvent) -> List[Violation]:
        """处理一行事件，返回本行产生的违规（可能为空或多条）。"""
        raw: RawEvent = event.raw
        step_no = len(self.transitions) + 1
        produced: List[Violation] = []
        note = ""

        if raw.action not in VALID_ACTIONS:
            v = self._violation(
                "UNKNOWN_ACTION", event, f"未知 action={raw.action!r}，仅支持 OPEN/CLOSE"
            )
            produced.append(v)
            note = "rejected: unknown action"
        elif raw.action == "OPEN":
            pred_violations = self._check_predecessors(event)
            produced.extend(pred_violations)
            # 返工重入：操作已出现过，且存在一条“前序已完成 -> 本操作”的返工边。
            # 普通边的并行/重复 OPEN 不计入返工。
            if raw.operation in self.seen:
                rework_preds = self.spec.rework_predecessors(raw.operation)
                if any(self.completed.get(p, 0) > 0 for p in rework_preds):
                    self.rework_count += 1
                    note = "open (rework re-entry)"
                else:
                    note = "open (re-entry)"
            else:
                note = "open"
            self.seen.add(raw.operation)
            self.open_stacks.setdefault(raw.operation, []).append(event)
        else:  # CLOSE
            stack = self.open_stacks.get(raw.operation)
            if not stack:
                v = self._violation(
                    "CLOSE_WITHOUT_OPEN",
                    event,
                    f"操作 {raw.operation} 的 CLOSE 没有可配对的 OPEN",
                )
                produced.append(v)
                note = "rejected: close without open"
            else:
                stack.pop()
                self.completed[raw.operation] = self.completed.get(raw.operation, 0) + 1
                self.last_close = event
                note = "close (paired)"

        self.transitions.append(
            _Transition(
                step=step_no,
                source_event_id=raw.source_event_id,
                line_code=raw.line_code,
                row_number=raw.row_number,
                operation=raw.operation,
                action=raw.action,
                accepted=not produced,
                note=note,
            )
        )
        return produced

    # ------------------------------------------------------------------ #
    def finalize(self) -> Tuple[Tuple[Tuple[RawEvent, str], ...], List[Violation]]:
        """轨迹结束：栈中残留的 OPEN 即悬空操作；检查必需工序缺失。"""
        dangling: List[Tuple[RawEvent, str]] = []
        violations: List[Violation] = []
        for op in sorted(self.open_stacks):
            for ev in self.open_stacks[op]:
                dangling.append((ev.raw, "OPEN_WITHOUT_CLOSE"))

        required = self.spec.required or frozenset(
            op for op in self.spec.all_operations() if op not in self.spec.optional
        )
        for op in sorted(required):
            if op not in self.seen:
                violations.append(
                    Violation(
                        synthetic_batch_id=self.batch_id,
                        violation_type="MISSING_REQUIRED",
                        blocking_edge=None,
                        operation=op,
                        action="",
                        source_event_id=None,
                        line_code=None,
                        row_number=None,
                        package_id=None,
                        detail=f"必需工序 {op} 在整条轨迹中从未出现",
                    )
                )
        return tuple(dangling), violations


def interpret_group(spec: ProcessSpec, group: LinkedGroup) -> SyntheticBatch:
    """对一个关联组运行状态机，产出完整的合成批次分析对象。"""
    sm = BatchStateMachine(spec, group.synthetic_batch_id)

    violations: List[Violation] = []
    wait_samples: List[WaitSample] = []
    trace: List[str] = []

    for event in group.events:
        raw = event.raw
        trace.append(f"{raw.operation}:{raw.action}")

        if raw.action == "OPEN" and sm.last_close is not None:
            wait_seconds = (event.utc_time - sm.last_close.utc_time).total_seconds()
            wait_samples.append(
                WaitSample(
                    synthetic_batch_id=group.synthetic_batch_id,
                    line_code=raw.line_code,
                    operation=raw.operation,
                    wait_seconds=wait_seconds,
                    open_event_id=raw.source_event_id,
                    prior_close_event_id=sm.last_close.raw.source_event_id,
                )
            )

        violations.extend(sm.step(event))

    dangling, final_violations = sm.finalize()
    violations.extend(final_violations)

    return SyntheticBatch(
        synthetic_batch_id=group.synthetic_batch_id,
        members=group.members,
        events=group.events,
        trace=tuple(trace),
        unassigned=group.unassigned,
        unassigned_reason=group.unassigned_reason,
        dangling=dangling,
        violations=tuple(violations),
        wait_samples=tuple(wait_samples),
        rework_count=sm.rework_count,
    )
