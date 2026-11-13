"""状态机单元测试：逐行解释轨迹，核对路径统计与违规行位置。

不依赖合成材料，手工构造最小事件序列，便于人工逐行审计。
"""
from __future__ import annotations

from datetime import datetime, timezone

from mes_pipeline.config import ProcessSpec
from mes_pipeline.linking import LinkedGroup
from mes_pipeline.models import CalibratedEvent, LineBatch, RawEvent
from mes_pipeline.state_machine import BatchStateMachine, interpret_group

UTC = timezone.utc
SPEC = ProcessSpec(
    edges=frozenset(
        {
            ("START", "CUT"),
            ("CUT", "WELD-A"),
            ("CUT", "WELD-B"),
            ("WELD-A", "INSPECT"),
            ("WELD-B", "INSPECT"),
            ("INSPECT", "PACK"),
        }
    ),
    rework_edges=frozenset({("INSPECT", "CUT")}),
    optional=frozenset(),
    required=frozenset({"START", "CUT", "WELD-A", "WELD-B", "INSPECT", "PACK"}),
)


def _ev(row: int, op: str, action: str, minute: int, sid: str | None = None) -> CalibratedEvent:
    raw = RawEvent(
        source_event_id=sid or f"E{row}",
        line_code="LINE-A",
        batch_label="T",
        operation=op,
        action=action,
        wall_clock=f"2026-11-01 00:{minute:02d}:00",
        row_number=row,
        package_id="pkg",
        handover_barcode="BC",
    )
    return CalibratedEvent(raw=raw, utc_time=datetime(2026, 11, 1, 0, minute, tzinfo=UTC))


def _group(events: list[CalibratedEvent]) -> LinkedGroup:
    return LinkedGroup(
        synthetic_batch_id="SB-test",
        members=frozenset({LineBatch("LINE-A", "T")}),
        events=tuple(events),
        unassigned=False,
        unassigned_reason="",
        applied_withdrawal_ids=(),
    )


def test_happy_path_no_violation():
    events = [
        _ev(1, "START", "OPEN", 0), _ev(2, "START", "CLOSE", 1),
        _ev(3, "CUT", "OPEN", 2), _ev(4, "CUT", "CLOSE", 3),
        _ev(5, "WELD-A", "OPEN", 4), _ev(6, "WELD-A", "CLOSE", 5),
        _ev(7, "WELD-B", "OPEN", 4), _ev(8, "WELD-B", "CLOSE", 6),
        _ev(9, "INSPECT", "OPEN", 7), _ev(10, "INSPECT", "CLOSE", 8),
        _ev(11, "PACK", "OPEN", 9), _ev(12, "PACK", "CLOSE", 10),
    ]
    batch = interpret_group(SPEC, _group(events))
    assert batch.violations == ()
    assert batch.dangling == ()
    assert batch.trace[0] == "START:OPEN"
    assert batch.trace[-1] == "PACK:CLOSE"


def test_open_without_close_is_dangling_and_no_duration_guess():
    events = [
        _ev(1, "START", "OPEN", 0), _ev(2, "START", "CLOSE", 1),
        _ev(3, "CUT", "OPEN", 2),  # 悬空：没有 CUT CLOSE
    ]
    batch = interpret_group(SPEC, _group(events))
    assert len(batch.dangling) == 1
    raw, reason = batch.dangling[0]
    assert reason == "OPEN_WITHOUT_CLOSE"
    assert raw.operation == "CUT" and raw.row_number == 3
    # 悬空不估算加工时长：等待样本只基于已发生的 OPEN/CLOSE 边界
    waits = {(w.operation, w.wait_seconds) for w in batch.wait_samples}
    assert waits == {("CUT", 60.0)}  # 仅 START CLOSE -> CUT OPEN


def test_close_without_open_violation_points_at_row():
    events = [
        _ev(1, "START", "OPEN", 0), _ev(2, "START", "CLOSE", 1),
        _ev(3, "CUT", "CLOSE", 2),  # 无 OPEN 的 CLOSE
    ]
    batch = interpret_group(SPEC, _group(events))
    v = [x for x in batch.violations if x.violation_type == "CLOSE_WITHOUT_OPEN"]
    assert len(v) == 1
    assert v[0].row_number == 3
    assert v[0].operation == "CUT"


def test_missing_predecessor_blocking_edge_and_row():
    events = [
        _ev(1, "START", "OPEN", 0), _ev(2, "START", "CLOSE", 1),
        # 跳过 CUT，直接 WELD-A
        _ev(3, "WELD-A", "OPEN", 2),
    ]
    batch = interpret_group(SPEC, _group(events))
    missing = [v for v in batch.violations if v.violation_type == "MISSING_PREDECESSOR"]
    assert any(v.blocking_edge == ("CUT", "WELD-A") and v.row_number == 3 for v in missing)


def test_parallel_branch_both_predecessors_required():
    events = [
        _ev(1, "START", "OPEN", 0), _ev(2, "START", "CLOSE", 1),
        _ev(3, "CUT", "OPEN", 2), _ev(4, "CUT", "CLOSE", 3),
        _ev(5, "WELD-A", "OPEN", 4), _ev(6, "WELD-A", "CLOSE", 5),
        # 缺 WELD-B
        _ev(7, "INSPECT", "OPEN", 6),
    ]
    batch = interpret_group(SPEC, _group(events))
    edges = {v.blocking_edge for v in batch.violations if v.violation_type == "MISSING_PREDECESSOR"}
    assert ("WELD-B", "INSPECT") in edges


def test_legal_rework_via_rework_edge_is_not_violation():
    events = [
        _ev(1, "START", "OPEN", 0), _ev(2, "START", "CLOSE", 1),
        _ev(3, "CUT", "OPEN", 2), _ev(4, "CUT", "CLOSE", 3),
        _ev(5, "WELD-A", "OPEN", 4), _ev(6, "WELD-A", "CLOSE", 5),
        _ev(7, "WELD-B", "OPEN", 4), _ev(8, "WELD-B", "CLOSE", 6),
        _ev(9, "INSPECT", "OPEN", 7), _ev(10, "INSPECT", "CLOSE", 8),
        # 返工：INSPECT->CUT 是 rework_edge，CUT 再次 OPEN 不触发前序违规
        _ev(11, "CUT", "OPEN", 9), _ev(12, "CUT", "CLOSE", 10),
    ]
    batch = interpret_group(SPEC, _group(events))
    assert batch.rework_count == 1
    assert all(v.violation_type != "MISSING_PREDECESSOR" for v in batch.violations)


def test_missing_required_operation_reported_without_row():
    events = [_ev(1, "START", "OPEN", 0), _ev(2, "START", "CLOSE", 1)]
    batch = interpret_group(SPEC, _group(events))
    missing_req = [v for v in batch.violations if v.violation_type == "MISSING_REQUIRED"]
    ops = {v.operation for v in missing_req}
    assert {"CUT", "WELD-A", "WELD-B", "INSPECT", "PACK"} <= ops
    assert all(v.row_number is None and v.blocking_edge is None for v in missing_req)


def test_step_by_step_transition_audit_trail():
    """逐行驱动状态机，核对每一步的转移解释。"""
    sm = BatchStateMachine(SPEC, "SB-test")
    e1, e2, e3 = _ev(1, "START", "OPEN", 0), _ev(2, "START", "CLOSE", 1), _ev(3, "CUT", "OPEN", 2)
    assert sm.step(e1) == []
    assert sm.step(e2) == []
    assert sm.step(e3) == []
    assert sm.transitions[0].note == "open"
    assert sm.transitions[1].note == "close (paired)"
    assert sm.completed == {"START": 1}
    dangling, missing = sm.finalize()
    assert {r.operation for r, _ in dangling} == {"CUT"}


def test_unknown_action_rejected():
    sm = BatchStateMachine(SPEC, "SB-test")
    violations = sm.step(_ev(1, "START", "PAUSE", 0))
    assert violations[0].violation_type == "UNKNOWN_ACTION"
    assert sm.open_stacks == {}


def test_wait_sample_uses_prior_close_boundary():
    events = [
        _ev(1, "START", "OPEN", 0), _ev(2, "START", "CLOSE", 2),
        _ev(3, "CUT", "OPEN", 5),
    ]
    batch = interpret_group(SPEC, _group(events))
    assert len(batch.wait_samples) == 1
    sample = batch.wait_samples[0]
    assert sample.operation == "CUT"
    assert sample.wait_seconds == 180.0
    assert sample.open_event_id == "E3" and sample.prior_close_event_id == "E2"


def test_optional_step_missing_does_not_block_or_violate():
    # CUT -> [可选 DEBURR] -> PACK，且 CUT->PACK 有直连边
    spec = ProcessSpec(
        edges=frozenset({("CUT", "DEBURR"), ("DEBURR", "PACK"), ("CUT", "PACK")}),
        rework_edges=frozenset(),
        optional=frozenset({"DEBURR"}),
        required=frozenset({"CUT", "PACK"}),
    )
    events = [
        _ev(1, "CUT", "OPEN", 0), _ev(2, "CUT", "CLOSE", 1),
        _ev(3, "PACK", "OPEN", 2), _ev(4, "PACK", "CLOSE", 3),
    ]
    batch = interpret_group(spec, _group(events))
    assert batch.violations == ()
    assert batch.dangling == ()
