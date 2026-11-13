"""实耗批次谱系计算引擎（纯标准库、精确有理数算术）。

适用假设（结果文件中同样明示）：

1. **理想均匀混合**：每个批次内部组成处处相同；部分领用按该批各外购原料的
   质量占比分摊，不假设领走的是某一层/某一桶。
2. 同一次操作的全部产出与损耗，按该操作**全部投入**的混合组成等比例分配；
   质量损耗只减少数量，不选择性去除某种原料。
3. 分流后再汇合（菱形路径）时，同一外购原料沿两支的贡献**相加**：每个贡献
   自带完整路径，路径不同则分别保留，绝不按原料 ID 去重丢支，也不把整支
   祖先批量重复计算。
4. 未领用部分保持原批组成。
5. 全部质量以 :class:`fractions.Fraction` 精确运算；质量守恒、超领、余额、
   召回总量均无浮点容差。

引擎只接受 :mod:`mes_pipeline.lineage.io` 校验通过的输入，自身不再做单据级
容错；计算顺序由生产者/消费者依赖的拓扑序决定，与文件行序无关。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Mapping, Sequence, Tuple

from .models import Operation, RawBatch

ZERO = Fraction(0)
ONE = Fraction(1)


# --------------------------------------------------------------------------- #
# 结果值对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ContributionPath:
    """一条外购原料贡献的流经证据。

    - ``batch_chain``：从外购原料批到当前批依次经过的批 ID（含首尾）；
    - ``operation_chain``：依次经过的 operation_id；
    - ``mass_g``：沿**这条具体路径**到达当前批的该原料质量（克）。
    """

    batch_chain: Tuple[str, ...]
    operation_chain: Tuple[str, ...]
    mass_g: Fraction


@dataclass(frozen=True)
class Contribution:
    """某外购原料批对当前批的质量贡献（可能由多条菱形路径汇合而成）。"""

    raw_batch_id: str
    mass_g: Fraction                       # 各路径质量之和
    paths: Tuple[ContributionPath, ...]

    @property
    def operation_ids(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for p in self.paths:
            for op in p.operation_chain:
                if op not in seen:
                    seen.append(op)
        return tuple(seen)


@dataclass(frozen=True)
class BatchRecord:
    """一个已知批的完整谱系记录。"""

    batch_id: str
    kind: str                              # "raw" 外购 / "produced" 产出
    material: str
    produced_mass_g: Fraction              # 生成质量（外购批=初始质量）
    consumed_mass_g: Fraction              # 累计实耗
    remaining_mass_g: Fraction             # 在库余额
    producer_operation: str | None
    generated_composition: Tuple[Contribution, ...]  # 生成时按外购批拆分的构成
    composition: Tuple[Contribution, ...]            # 当前余额的构成（耗尽则各项为 0）
    consumption: Tuple[Tuple[str, Fraction], ...]  # (operation_id, 实耗 g)
    is_exhausted: bool


@dataclass(frozen=True)
class LossRecord:
    """一次操作的损耗按外购原料的构成分配（损耗不选择性去除原料）。"""

    operation_id: str
    total_loss_g: Fraction
    composition: Tuple[Contribution, ...]


@dataclass(frozen=True)
class LedgerEntry:
    """某外购原料批的全局质量账：初始 = 全部余额贡献 + 累计损耗贡献。"""

    raw_batch_id: str
    initial_g: Fraction
    remaining_contribution_g: Fraction
    loss_contribution_g: Fraction
    balanced: bool


@dataclass(frozen=True)
class RecallItem:
    """召回结果中的一个在库批。"""

    batch_id: str
    material: str
    remaining_mass_g: Fraction            # 可用质量（整批隔离量同此值）
    suspect_mass_g: Fraction              # 其中可疑原料质量
    suspect_share: Fraction               # 可疑原料占比（精确分数）
    suspect_raw_batches: Tuple[str, ...]
    evidence: Tuple[Contribution, ...]    # 逐可疑原料的路径依据


@dataclass(frozen=True)
class RecallResult:
    suspect_raw_batches: Tuple[str, ...]
    items: Tuple[RecallItem, ...]
    total_quarantine_g: Fraction          # 每个在库批最多计一次
    ledgers: Tuple[LedgerEntry, ...]      # 可疑原料的全局账核对


@dataclass(frozen=True)
class LineageResult:
    raw_batches: Tuple[RawBatch, ...]
    operations: Tuple[Operation, ...]
    batches: Tuple[BatchRecord, ...]
    losses: Tuple[LossRecord, ...]
    ledger: Tuple[LedgerEntry, ...]
    assumptions: Tuple[str, ...]


ASSUMPTIONS = (
    "理想均匀混合：每个批次内部组成相同，部分领用按该批各外购原料质量占比分摊。",
    "同一操作的各产出与损耗按该操作全部投入的混合组成等比例分配；"
    "质量损耗只减少数量，不选择性去除某种原料。",
    "分流后汇合的菱形路径上，同一外购原料沿各支的贡献相加，不去重、不重复整支祖先量。",
    "未领用部分保持原批组成；全部质量采用有理数精确算术，守恒/超领/余额判定无浮点容差。",
    "谱系仅依据外购批声明与实际领料/产出操作记录建立，"
    "不从工序轨迹、配料建议或量具试验推测实际用料。",
)


# --------------------------------------------------------------------------- #
# 拓扑序（依赖已在 io 层保证无环；这里做确定性的 Kahn 排序）
# --------------------------------------------------------------------------- #
def _topological_order(operations: Sequence[Operation],
                       producer: Mapping[str, Operation]) -> List[Operation]:
    by_id = {op.operation_id: op for op in operations}
    indegree = {op.operation_id: 0 for op in operations}
    adjacency: Dict[str, List[str]] = {op.operation_id: [] for op in operations}
    for op in operations:
        for line in op.inputs:
            src = producer.get(line.batch_id)
            if src is not None and src.operation_id != op.operation_id:
                adjacency[src.operation_id].append(op.operation_id)
                indegree[op.operation_id] += 1
    ready = sorted(oid for oid, deg in indegree.items() if deg == 0)
    ordered: List[Operation] = []
    while ready:
        oid = ready.pop(0)
        ordered.append(by_id[oid])
        for nxt in sorted(adjacency[oid]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    if len(ordered) != len(operations):  # 理论不可达：io 层已拒绝成环
        raise RuntimeError("内部错误：操作依赖仍存在环")
    return ordered


def _merge(contribs: Sequence[Contribution]) -> Tuple[Contribution, ...]:
    """按外购原料批合并贡献；不同路径分别保留、同路径质量相加。"""
    merged: Dict[str, Dict[Tuple[Tuple[str, ...], Tuple[str, ...]], Fraction]] = {}
    order: List[str] = []
    for c in contribs:
        if c.raw_batch_id not in merged:
            merged[c.raw_batch_id] = {}
            order.append(c.raw_batch_id)
        bucket = merged[c.raw_batch_id]
        for p in c.paths:
            key = (p.batch_chain, p.operation_chain)
            bucket[key] = bucket.get(key, ZERO) + p.mass_g
    out: List[Contribution] = []
    for raw_id in order:
        paths = tuple(
            ContributionPath(batch_chain=k[0], operation_chain=k[1], mass_g=m)
            for k, m in sorted(merged[raw_id].items()))
        out.append(Contribution(raw_id,
                                mass_g=sum((p.mass_g for p in paths), ZERO),
                                paths=paths))
    return tuple(out)


# --------------------------------------------------------------------------- #
# 谱系构建
# --------------------------------------------------------------------------- #
def build_lineage(raw_batches: Sequence[RawBatch],
                  operations: Sequence[Operation]) -> LineageResult:
    producer: Dict[str, Operation] = {}
    for op in operations:
        for line in op.outputs:
            producer[line.batch_id] = op

    # 当前每批“满批状态”的组成（按外购原料拆分，含完整路径）
    composition: Dict[str, Tuple[Contribution, ...]] = {}
    material_of: Dict[str, str] = {}
    produced_mass: Dict[str, Fraction] = {}
    for rb in raw_batches:
        path = ContributionPath(batch_chain=(rb.batch_id,),
                                operation_chain=(), mass_g=rb.initial.grams)
        composition[rb.batch_id] = (
            Contribution(rb.batch_id, rb.initial.grams, (path,)),)
        material_of[rb.batch_id] = rb.material
        produced_mass[rb.batch_id] = rb.initial.grams

    consumed: Dict[str, Fraction] = {rb.batch_id: ZERO for rb in raw_batches}
    consumption_detail: Dict[str, List[Tuple[str, Fraction]]] = {
        rb.batch_id: [] for rb in raw_batches}
    loss_records: List[LossRecord] = []

    for op in _topological_order(operations, producer):
        total_in = op.input_total_g
        # 1) 汇总本操作全部投入的混合组成（部分领用按各输入批占比分摊）
        pooled: List[Contribution] = []
        for line in op.inputs:
            scale = line.qty.grams / produced_mass[line.batch_id]
            consumed[line.batch_id] += line.qty.grams
            consumption_detail.setdefault(line.batch_id, []).append(
                (op.operation_id, line.qty.grams))
            for c in composition[line.batch_id]:
                scaled_paths = tuple(
                    ContributionPath(
                        batch_chain=p.batch_chain,
                        operation_chain=p.operation_chain,
                        mass_g=p.mass_g * scale,
                    ) for p in c.paths)
                pooled.append(Contribution(c.raw_batch_id,
                                           c.mass_g * scale, scaled_paths))
        input_composition = _merge(pooled)
        # 守恒断言：投入组成各原料之和必须等于投入总量
        assert sum((c.mass_g for c in input_composition), ZERO) == total_in

        # 2) 各产出按产出质量占总投入的比例分配同一混合组成
        for out_line in op.outputs:
            out_scale = out_line.qty.grams / total_in
            out_contribs = tuple(
                Contribution(
                    c.raw_batch_id, c.mass_g * out_scale,
                    tuple(ContributionPath(
                        batch_chain=p.batch_chain + (out_line.batch_id,),
                        operation_chain=p.operation_chain + (op.operation_id,),
                        mass_g=p.mass_g * out_scale) for p in c.paths),
                ) for c in input_composition)
            composition[out_line.batch_id] = out_contribs
            produced_mass[out_line.batch_id] = out_line.qty.grams
            consumed.setdefault(out_line.batch_id, ZERO)
            consumption_detail.setdefault(out_line.batch_id, [])
            material_of[out_line.batch_id] = out_line.batch_id

        # 3) 损耗按同一混合组成分配（只减量、不选择性去除原料）
        if op.loss.grams > 0:
            loss_scale = op.loss.grams / total_in
            loss_contribs = tuple(
                Contribution(
                    c.raw_batch_id, c.mass_g * loss_scale,
                    tuple(ContributionPath(
                        batch_chain=p.batch_chain + (f"LOSS@{op.operation_id}",),
                        operation_chain=p.operation_chain + (op.operation_id,),
                        mass_g=p.mass_g * loss_scale) for p in c.paths),
                ) for c in input_composition)
            loss_records.append(LossRecord(op.operation_id, op.loss.grams,
                                           loss_contribs))

    # 4) 余额：未领用部分保持原组成
    batch_records: List[BatchRecord] = []
    all_ids = sorted(set(composition) | set(consumed))
    for bid in all_ids:
        full_mass = produced_mass[bid]
        used = consumed.get(bid, ZERO)
        remaining = full_mass - used
        remain_scale = remaining / full_mass if full_mass > 0 else ZERO
        remain_composition = tuple(
            Contribution(
                c.raw_batch_id, c.mass_g * remain_scale,
                tuple(ContributionPath(p.batch_chain, p.operation_chain,
                                       p.mass_g * remain_scale)
                      for p in c.paths),
            ) for c in composition[bid])
        details = tuple(sorted(consumption_detail.get(bid, [])))
        batch_records.append(BatchRecord(
            batch_id=bid,
            kind="raw" if any(rb.batch_id == bid for rb in raw_batches) else "produced",
            material=material_of.get(bid, bid),
            produced_mass_g=full_mass,
            consumed_mass_g=used,
            remaining_mass_g=remaining,
            producer_operation=(producer[bid].operation_id if bid in producer else None),
            generated_composition=composition[bid],
            composition=remain_composition,
            consumption=details,
            is_exhausted=(remaining == 0),
        ))

    # 5) 全局账：每个外购原料 初始 = 目前所有批剩余贡献 + 累计损耗贡献
    loss_by_raw: Dict[str, Fraction] = {}
    for rec in loss_records:
        for c in rec.composition:
            loss_by_raw[c.raw_batch_id] = loss_by_raw.get(c.raw_batch_id, ZERO) + c.mass_g
    ledger: List[LedgerEntry] = []
    for rb in sorted(raw_batches, key=lambda b: b.batch_id):
        remaining_sum = ZERO
        for rec in batch_records:
            for c in rec.composition:
                if c.raw_batch_id == rb.batch_id:
                    remaining_sum += c.mass_g
        loss_sum = loss_by_raw.get(rb.batch_id, ZERO)
        ledger.append(LedgerEntry(
            raw_batch_id=rb.batch_id,
            initial_g=rb.initial.grams,
            remaining_contribution_g=remaining_sum,
            loss_contribution_g=loss_sum,
            balanced=(rb.initial.grams == remaining_sum + loss_sum),
        ))

    return LineageResult(
        raw_batches=tuple(sorted(raw_batches, key=lambda b: b.batch_id)),
        operations=tuple(operations),
        batches=tuple(batch_records),
        losses=tuple(loss_records),
        ledger=tuple(ledger),
        assumptions=ASSUMPTIONS,
    )


# --------------------------------------------------------------------------- #
# 构成查询与召回
# --------------------------------------------------------------------------- #
def query_composition(result: LineageResult, batch_id: str) -> BatchRecord:
    for rec in result.batches:
        if rec.batch_id == batch_id:
            return rec
    known = sorted(r.batch_id for r in result.batches)
    raise KeyError(f"未知批次 {batch_id!r}（已知: {known}）")


def run_recall(result: LineageResult,
               suspect_raw_batches: Sequence[str]) -> RecallResult:
    suspects = tuple(dict.fromkeys(s for s in suspect_raw_batches))  # 保序去重
    raw_ids = {rb.batch_id for rb in result.raw_batches}
    unknown = sorted(set(suspects) - raw_ids)
    if unknown:
        raise KeyError(f"可疑外购批不存在: {unknown}")
    if not suspects:
        raise ValueError("召回至少指定一个可疑外购批")

    items: List[RecallItem] = []
    for rec in result.batches:
        if rec.remaining_mass_g <= 0:
            continue  # 已完全耗尽的中间批不加入隔离总量
        hit = tuple(c for c in rec.composition
                    if c.raw_batch_id in set(suspects) and c.mass_g > 0)
        if not hit:
            continue
        suspect_mass = sum((c.mass_g for c in hit), ZERO)
        items.append(RecallItem(
            batch_id=rec.batch_id,
            material=rec.material,
            remaining_mass_g=rec.remaining_mass_g,
            suspect_mass_g=suspect_mass,
            suspect_share=suspect_mass / rec.remaining_mass_g,
            suspect_raw_batches=tuple(c.raw_batch_id for c in hit),
            evidence=hit,
        ))

    items.sort(key=lambda it: it.batch_id)
    total = sum((it.remaining_mass_g for it in items), ZERO)  # 同批只隔离一次
    ledgers = tuple(entry for entry in result.ledger
                    if entry.raw_batch_id in set(suspects))
    return RecallResult(
        suspect_raw_batches=suspects,
        items=tuple(items),
        total_quarantine_g=total,
        ledgers=ledgers,
    )
