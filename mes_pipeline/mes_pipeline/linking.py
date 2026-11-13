"""合成批次关联。

规则（硬性，不做字符串近似配对）：
- batch_label 仅在产线内唯一，跨产线只凭交接条码 handover_barcode 关联；
- 条码缺失 -> 未归属批次（MISSING_BARCODE）；
- 多对一歧义 -> 同一产线内有 >=2 个产线批次共用同一条码（交接条码在每条产线
  上只能标识一个批次），相关批次全部列为未归属（AMBIGUOUS_BARCODE）；
  不同产线各持有一次同一条码是合法 1:1 交接，正常并批；
- 撤回单按 source_event_id 逻辑删除事件，撤回单号保留；
- 条码修正按 (line_code, batch_label) 定位，old_barcode 必须与现值一致，
  修正后整库重建关联（天然支持修正引发的批次拆分）。
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from .models import (
    BarcodeCorrection,
    CalibratedEvent,
    LineBatch,
    RawEvent,
    Withdrawal,
)


class LinkError(ValueError):
    """关联阶段的数据错误。"""


@dataclass(frozen=True)
class LinkedGroup:
    """关联结果的中间结构，供状态机消费。"""

    synthetic_batch_id: str
    members: frozenset[LineBatch]
    events: Tuple[CalibratedEvent, ...]
    unassigned: bool
    unassigned_reason: str
    applied_withdrawal_ids: Tuple[str, ...]


# --------------------------------------------------------------------------- #
# 修正与撤回
# --------------------------------------------------------------------------- #
def apply_barcode_corrections(
    events: Sequence[CalibratedEvent],
    corrections: Sequence[BarcodeCorrection],
) -> Tuple[Tuple[CalibratedEvent, ...], Tuple[str, ...]]:
    """返回应用修正后的新事件序列（不可变模型 -> 重建对象）与生效的修正单号。"""
    index: Dict[Tuple[str, str], List[BarcodeCorrection]] = defaultdict(list)
    for corr in corrections:
        index[(corr.line_code, corr.batch_label)].append(corr)

    applied: List[str] = []
    out: List[CalibratedEvent] = []
    for ev in events:
        raw = ev.raw
        chain = sorted(index.get((raw.line_code, raw.batch_label), ()),
                       key=lambda c: c.correction_id)
        current_barcode = raw.handover_barcode
        for corr in chain:
            if current_barcode != corr.old_barcode:
                raise LinkError(
                    f"条码修正 {corr.correction_id} 无法应用: "
                    f"{raw.line_code}/{raw.batch_label} 当前条码为 "
                    f"{current_barcode!r}，修正单要求旧值 {corr.old_barcode!r}"
                )
            current_barcode = corr.new_barcode
            if corr.correction_id not in applied:
                applied.append(corr.correction_id)
        if current_barcode != raw.handover_barcode:
            new_raw = RawEvent(
                source_event_id=raw.source_event_id,
                line_code=raw.line_code,
                batch_label=raw.batch_label,
                operation=raw.operation,
                action=raw.action,
                wall_clock=raw.wall_clock,
                row_number=raw.row_number,
                package_id=raw.package_id,
                handover_barcode=current_barcode,
            )
            out.append(CalibratedEvent(raw=new_raw, utc_time=ev.utc_time))
        else:
            out.append(ev)
    return tuple(out), tuple(applied)


def apply_withdrawals(
    events: Sequence[CalibratedEvent],
    withdrawals: Sequence[Withdrawal],
) -> Tuple[Tuple[CalibratedEvent, ...], Tuple[str, ...]]:
    """逻辑撤回事件。撤回单号保留；指向不存在记录的撤回单直接报错。"""
    by_sid: Dict[str, Withdrawal] = {}
    for w in withdrawals:
        if w.source_event_id in by_sid:
            raise LinkError(
                f"撤回单 {w.withdrawal_id} 与 {by_sid[w.source_event_id].withdrawal_id} "
                f"重复指向同一 source_event_id={w.source_event_id}"
            )
        by_sid[w.source_event_id] = w

    live_sids = {ev.raw.source_event_id for ev in events}
    missing = [w.withdrawal_id for w in withdrawals if w.source_event_id not in live_sids]
    if missing:
        raise LinkError(f"撤回单指向不存在的 source_event_id: {sorted(missing)}")

    withdrawn_ids = set(by_sid)
    kept = tuple(ev for ev in events if ev.raw.source_event_id not in withdrawn_ids)
    return kept, tuple(sorted(w.withdrawal_id for w in withdrawals))


# --------------------------------------------------------------------------- #
# 分组
# --------------------------------------------------------------------------- #
def _synthetic_id(members: Iterable[LineBatch]) -> str:
    canonical = "|".join(sorted(f"{m.line_code}/{m.batch_label}" for m in members))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"SB-{digest}"


def link_batches(
    events: Sequence[CalibratedEvent],
    withdrawals: Sequence[Withdrawal] = (),
    corrections: Sequence[BarcodeCorrection] = (),
) -> Tuple[Tuple[LinkedGroup, ...], dict]:
    """完整关联流程：修正 -> 撤回 -> 产线批次分组 -> 条码并查集 -> 未归属判定。"""
    events, applied_corrections = apply_barcode_corrections(events, corrections)
    events, applied_withdrawals = apply_withdrawals(events, withdrawals)

    # 1) 产线内批次分组
    line_batches: Dict[Tuple[str, str], List[CalibratedEvent]] = defaultdict(list)
    for ev in events:
        line_batches[(ev.raw.line_code, ev.raw.batch_label)].append(ev)

    batch_objs: Dict[Tuple[str, str], LineBatch] = {
        key: LineBatch(line_code=key[0], batch_label=key[1]) for key in line_batches
    }

    # 2) 每个产线批次出现过的条码集合
    batch_barcodes: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    barcode_owners: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for key, evs in line_batches.items():
        for ev in evs:
            bc = ev.raw.handover_barcode
            if bc is not None:
                batch_barcodes[key].add(bc)
                barcode_owners[bc].add(key)

    # 3) 多对一歧义：同一产线内一个条码被多个产线批次持有
    #    （跨产线各持有一次属于合法 1:1 交接，不算歧义）
    ambiguous_barcodes: Set[str] = set()
    for bc, owners in barcode_owners.items():
        per_line: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
        for key in owners:
            per_line[key[0]].add(key)
        if any(len(line_owners) > 1 for line_owners in per_line.values()):
            ambiguous_barcodes.add(bc)
    ambiguous_batches: Set[Tuple[str, str]] = set()
    for bc in ambiguous_barcodes:
        ambiguous_batches.update(barcode_owners[bc])
    # 同一产线批次自身出现多个不同条码，同样无法确定唯一交接关系
    for key, bcs in batch_barcodes.items():
        if len(bcs) > 1:
            ambiguous_batches.add(key)

    # 4) 并查集：唯一条码把多个产线批次并为一个合成批次
    parent: Dict[Tuple[str, str], Tuple[str, str]] = {k: k for k in line_batches}

    def find(x: Tuple[str, str]) -> Tuple[str, str]:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: Tuple[str, str], b: Tuple[str, str]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # 取字典序小者为根，保证确定性
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for bc, owners in barcode_owners.items():
        if bc in ambiguous_barcodes:
            continue  # 歧义条码不参与自动配对
        owners_list = sorted(owners)
        for other in owners_list[1:]:
            union(owners_list[0], other)

    components: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
    for key in line_batches:
        components[find(key)].append(key)

    # 5) 撤回单号回放到组（按事件 sid 反查）
    sid_to_withdrawal = {w.source_event_id: w.withdrawal_id for w in withdrawals}

    groups: List[LinkedGroup] = []
    unassigned_counter = 0
    for root in sorted(components):
        member_keys = sorted(components[root])
        members = frozenset(batch_objs[k] for k in member_keys)
        comp_events = sorted(
            (ev for k in member_keys for ev in line_batches[k]),
            key=lambda e: e.sort_key,
        )
        comp_ambiguous = any(k in ambiguous_batches for k in member_keys)
        missing_barcode = [
            k for k in member_keys if not batch_barcodes.get(k)
        ]

        reason = ""
        unassigned = False
        if comp_ambiguous:
            unassigned, reason = True, "AMBIGUOUS_BARCODE"
        elif missing_barcode:
            # 单产线批次且完全没有条码 -> 未归属；多成员组件理论上不会走到这里
            unassigned, reason = True, "MISSING_BARCODE"
            unassigned_counter += 1

        wd_ids = tuple(sorted({
            sid_to_withdrawal[ev.raw.source_event_id]
            for ev in comp_events
            if ev.raw.source_event_id in sid_to_withdrawal
        }))

        groups.append(
            LinkedGroup(
                synthetic_batch_id=_synthetic_id(members),
                members=members,
                events=tuple(comp_events),
                unassigned=unassigned,
                unassigned_reason=reason,
                applied_withdrawal_ids=wd_ids,
            )
        )

    meta = {
        "applied_correction_ids": applied_corrections,
        "applied_withdrawal_ids": applied_withdrawals,
        "ambiguous_barcodes": tuple(sorted(ambiguous_barcodes)),
        "withdrawn_event_ids": tuple(sorted(sid_to_withdrawal)),
        "synthetic_batch_count": len(groups),
        "unassigned_count": sum(1 for g in groups if g.unassigned),
    }
    return tuple(groups), meta
