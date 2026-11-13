"""关联逻辑测试：条码缺失/歧义、撤回、条码修正与拆分。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mes_pipeline.linking import LinkError, apply_barcode_corrections, apply_withdrawals, link_batches
from mes_pipeline.models import BarcodeCorrection, CalibratedEvent, RawEvent, Withdrawal

UTC = timezone.utc


def _ev(sid: str, line: str, label: str, barcode: str | None, row: int) -> CalibratedEvent:
    raw = RawEvent(
        source_event_id=sid, line_code=line, batch_label=label,
        operation="START", action="OPEN", wall_clock="x",
        row_number=row, package_id="pkg", handover_barcode=barcode,
    )
    return CalibratedEvent(raw=raw, utc_time=datetime(2026, 11, 1, tzinfo=UTC))


def test_missing_barcode_is_unassigned():
    events = [_ev("E1", "LINE-A", "B1", None, 1)]
    groups, meta = link_batches(events)
    assert len(groups) == 1
    assert groups[0].unassigned is True
    assert groups[0].unassigned_reason == "MISSING_BARCODE"
    assert meta["unassigned_count"] == 1


def test_ambiguous_barcode_marks_all_owners_unassigned():
    events = [
        _ev("E1", "LINE-A", "B1", "BC-DUP", 1),
        _ev("E2", "LINE-A", "B2", "BC-DUP", 2),
    ]
    groups, _ = link_batches(events)
    assert len(groups) == 2  # 不做自动配对，各自成组
    assert all(g.unassigned and g.unassigned_reason == "AMBIGUOUS_BARCODE" for g in groups)
    # 同一条码字符串绝不做近似自动配对
    assert groups[0].members != groups[1].members


def test_unique_barcode_joins_cross_line_batches():
    events = [
        _ev("E1", "LINE-A", "B1", "BC-1", 1),
        _ev("E2", "LINE-B", "X1", "BC-1", 1),
    ]
    groups, _ = link_batches(events)
    assert len(groups) == 1
    members = {(m.line_code, m.batch_label) for m in groups[0].members}
    assert members == {("LINE-A", "B1"), ("LINE-B", "X1")}
    assert groups[0].unassigned is False


def test_same_barcode_one_per_line_is_legal_handover_chain():
    # 三条产线各一个批次共用同一条码：合法 1:1 交接链
    events = [
        _ev("E1", "LINE-A", "B1", "BC-1", 1),
        _ev("E2", "LINE-B", "X1", "BC-1", 1),
        _ev("E3", "LINE-C", "Y1", "BC-1", 1),
    ]
    groups, _ = link_batches(events)
    assert len(groups) == 1
    assert groups[0].unassigned is False


def test_intra_line_duplicate_poisons_all_holders_conservatively():
    # L1 内 B1/B2 共用 BC-X 构成多对一；L2 的 X1 也持该条码，
    # 保守起见全部标记歧义，绝不自动猜测配对
    events = [
        _ev("E1", "LINE-A", "B1", "BC-X", 1),
        _ev("E2", "LINE-A", "B2", "BC-X", 2),
        _ev("E3", "LINE-B", "X1", "BC-X", 1),
    ]
    groups, _ = link_batches(events)
    assert all(g.unassigned and g.unassigned_reason == "AMBIGUOUS_BARCODE" for g in groups)
    assert len(groups) == 3


def test_withdrawal_removes_middle_event_and_keeps_id():
    events = [_ev("E1", "LINE-A", "B1", "BC", 1), _ev("E2", "LINE-A", "B1", "BC", 2)]
    kept, ids = apply_withdrawals(events, [Withdrawal("WD-9", "E1", "pkg")])
    assert [e.raw.source_event_id for e in kept] == ["E2"]
    assert ids == ("WD-9",)


def test_withdrawal_unknown_sid_raises():
    events = [_ev("E1", "LINE-A", "B1", "BC", 1)]
    with pytest.raises(LinkError):
        apply_withdrawals(events, [Withdrawal("WD-9", "GHOST", "pkg")])


def test_duplicate_withdrawal_target_raises():
    events = [_ev("E1", "LINE-A", "B1", "BC", 1)]
    ws = [Withdrawal("WD-1", "E1", "pkg"), Withdrawal("WD-2", "E1", "pkg")]
    with pytest.raises(LinkError):
        apply_withdrawals(events, ws)


def test_barcode_correction_splits_synthetic_batch():
    # 初始：两个不同产线批次经唯一条码并为一个合成批次（合法交接）
    events = [
        _ev("E1", "LINE-A", "B1", "BC-S", 1),
        _ev("E2", "LINE-B", "X1", "BC-S", 1),
    ]
    before, _ = link_batches(events)
    assert len(before) == 1

    corrections = [
        BarcodeCorrection("FIX-1", "LINE-A", "B1", "BC-S", "BC-NEW", "pkg-admin")
    ]
    after, meta = link_batches(events, corrections=corrections)
    assert "FIX-1" in meta["applied_correction_ids"]
    assert len(after) == 2  # 改码后拆成两个合成批次
    all_members = {
        (m.line_code, m.batch_label) for g in after for m in g.members
    }
    assert all_members == {("LINE-A", "B1"), ("LINE-B", "X1")}


def test_correction_with_wrong_old_value_raises():
    events = [_ev("E1", "LINE-A", "B1", "BC-S", 1)]
    bad = BarcodeCorrection("FIX-2", "LINE-A", "B1", "BC-OTHER", "BC-NEW", "pkg-admin")
    with pytest.raises(LinkError):
        link_batches(events, corrections=[bad])


def test_correction_can_clear_barcode_to_missing():
    events = [_ev("E1", "LINE-A", "B1", "BC-S", 1)]
    clear = BarcodeCorrection("FIX-3", "LINE-A", "B1", "BC-S", None, "pkg-admin")
    fixed, _ = apply_barcode_corrections(events, [clear])
    assert fixed[0].raw.handover_barcode is None
    groups, _ = link_batches(events, corrections=[clear])
    assert groups[0].unassigned is True
    assert groups[0].unassigned_reason == "MISSING_BARCODE"


def test_same_timestamp_stable_sort_by_row_number():
    ts = datetime(2026, 11, 1, tzinfo=UTC)
    e1 = CalibratedEvent(
        RawEvent("E1", "L", "B", "WELD-B", "OPEN", "x", 7, "pkg", "BC"), ts
    )
    e2 = CalibratedEvent(
        RawEvent("E2", "L", "B", "WELD-A", "OPEN", "x", 8, "pkg", "BC"), ts
    )
    groups, _ = link_batches([e2, e1])  # 故意乱序输入
    trace_events = groups[0].events
    assert [e.raw.source_event_id for e in trace_events] == ["E1", "E2"]
