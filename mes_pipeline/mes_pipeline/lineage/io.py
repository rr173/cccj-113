"""谱系输入的读取与装载期校验。

两份 JSON 文件（与 MES 工序轨迹、配料建议、量具试验完全无关）：

- 原料声明文件：顶层 ``{"batches": [{"batch_id", "material",
  "initial_mass": {"mass", "unit"}}, ...]}``
- 操作记录文件：顶层 ``{"operations": [{"operation_id",
  "inputs": [{"batch_id", "mass", "unit"}], "outputs": [...],
  "loss_mass": {"mass", "unit"}}, ...]}``

质量单位仅接受 ``g`` / ``kg``（可混写，1 kg = 1000 g）。数量必须是有限十进制：
接受十进制**字符串**（如 ``"0.1"``）与 JSON 整数；非整数 JSON 浮点会被拒绝，
因为二进制浮点无法精确表达十进制，后续守恒/超领判定不允许浮点容差。

记录顺序不代表加工顺序。本模块先做结构与单据级校验（重复标识、同操作内重复
列批、正量要求、质量守恒、引用存在性、产出批唯一生产者），再按生产者/消费者
建立操作间依赖并检测成环，最后按**累计口径**核对每个批的累计实耗不超过其
可用量（与文件行序无关，合法的倒序记录不会被拒）。所有问题一次性收集，
定位到原记录序号后整体拒绝本次装载，绝不跳过坏操作继续产出谱系。
"""
from __future__ import annotations

import hashlib
import json
import re
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .models import (
    MASS_UNITS,
    MassLine,
    MassQty,
    Operation,
    RawBatch,
    UNIT_G,
    UNIT_KG,
)

_G_PER_KG = 1000
_DECIMAL_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_QTY_KEYS = frozenset({"mass", "unit"})


class LineageInputError(ValueError):
    """输入数据不合法：携带全部问题描述（含原记录定位），拒绝本次装载。"""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("谱系输入不合法，拒绝装载:\n"
                         + "\n".join(f"- {p}" for p in self.problems))


def digest_files(paths: Sequence[str | Path]) -> str:
    """输入文件字节级 SHA-256（按文件名排序拼接），用于结果可追溯。"""
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _load_json(path: str | Path, label: str, problems: List[str]) -> Optional[dict]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"{label}: 文件不可读: {exc}")
        return None
    try:
        # 拒绝 NaN / Infinity 等非标准 JSON 常量
        raw = json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(
            ValueError(f"非法 JSON 常量 {c}")))
    except json.JSONDecodeError as exc:
        problems.append(f"{label}: JSON 解析失败（第{exc.lineno}行第{exc.colno}列）: {exc.msg}")
        return None
    except ValueError as exc:
        problems.append(f"{label}: {exc}")
        return None
    if not isinstance(raw, dict):
        problems.append(f"{label}: 顶层必须是对象")
        return None
    return raw


def _parse_qty(raw: object, where: str, problems: List[str], *,
               allow_zero: bool = False) -> Optional[MassQty]:
    """解析 {"mass", "unit"}；任何不合法都写入 problems 并返回 None。"""
    if not isinstance(raw, dict):
        problems.append(f"{where}: 质量必须是含 mass/unit 的对象，得到 {type(raw).__name__}")
        return None
    unknown = set(raw) - _QTY_KEYS
    if unknown:
        problems.append(f"{where}: 含未知字段 {sorted(unknown)}（仅允许 mass/unit）")
    unit = raw.get("unit")
    if not isinstance(unit, str) or unit.strip().lower() not in MASS_UNITS:
        problems.append(f"{where}: 单位不明: {unit!r}（应为 g/kg）")
        return None
    unit = unit.strip().lower()
    mass_raw = raw.get("mass")
    raw_text: str
    if isinstance(mass_raw, bool):  # bool 是 int 的子类，先排除
        problems.append(f"{where}: mass 不能是布尔值: {mass_raw!r}")
        return None
    if isinstance(mass_raw, int):
        raw_text = str(mass_raw)
        value = Fraction(mass_raw)
    elif isinstance(mass_raw, str) and _DECIMAL_RE.match(mass_raw.strip()):
        raw_text = mass_raw.strip()
        value = Fraction(raw_text)
    elif isinstance(mass_raw, float):
        problems.append(f"{where}: mass 不接受浮点字面量 {mass_raw!r}，"
                        f"请改用十进制字符串（如 \"0.1\"）以保证精确核算")
        return None
    else:
        problems.append(f"{where}: mass 必须是有限十进制文本或整数: {mass_raw!r}")
        return None
    if value < 0 or (value == 0 and not allow_zero):
        rule = "必须为非负" if allow_zero else "必须为正"
        problems.append(f"{where}: 质量{rule}: {raw_text} {unit}")
        return None
    grams = value if unit == UNIT_G else value * _G_PER_KG
    return MassQty(grams=grams, unit=unit, raw_text=raw_text)


# --------------------------------------------------------------------------- #
# 原料声明
# --------------------------------------------------------------------------- #
def load_materials(path: str | Path, problems: List[str]) -> List[RawBatch]:
    label = Path(path).name
    raw = _load_json(path, label, problems)
    if raw is None:
        return []
    unknown = set(raw) - {"batches"}
    if unknown:
        problems.append(f"{label}: 顶层含未知字段 {sorted(unknown)}")
    records = raw.get("batches")
    if not isinstance(records, list):
        problems.append(f"{label}: 顶层 batches 必须是数组")
        return []

    batches: List[RawBatch] = []
    seen: Set[str] = set()
    for idx, item in enumerate(records, start=1):
        where = f"{label} 原料记录#{idx}"
        if not isinstance(item, dict):
            problems.append(f"{where}: 记录必须是对象")
            continue
        extra = set(item) - {"batch_id", "material", "initial_mass"}
        if extra:
            problems.append(f"{where}: 含未知字段 {sorted(extra)}")
        batch_id = item.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip():
            problems.append(f"{where}: batch_id 缺失或为空")
            continue
        batch_id = batch_id.strip()
        if batch_id in seen:
            problems.append(f"{where}: batch_id 重复: {batch_id}")
            continue
        material = item.get("material")
        if not isinstance(material, str) or not material.strip():
            problems.append(f"{where}: material 缺失或为空")
            continue
        qty = _parse_qty(item.get("initial_mass"), f"{where} initial_mass", problems)
        if qty is None:
            continue
        seen.add(batch_id)
        batches.append(RawBatch(batch_id=batch_id, material=material.strip(),
                                initial=qty, row=idx))
    return batches


# --------------------------------------------------------------------------- #
# 操作记录
# --------------------------------------------------------------------------- #
def _parse_lines(raw: object, where: str, problems: List[str],
                 op_id: str) -> Optional[List[MassLine]]:
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where}: {op_id} 的该列表必须是非空数组")
        return None
    lines: List[MassLine] = []
    local_seen: Set[str] = set()
    for j, item in enumerate(raw, start=1):
        line_where = f"{where} 第{j}行"
        if not isinstance(item, dict):
            problems.append(f"{line_where}: 记录必须是对象")
            continue
        extra = set(item) - {"batch_id", "mass", "unit"}
        if extra:
            problems.append(f"{line_where}: 含未知字段 {sorted(extra)}")
        batch_id = item.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip():
            problems.append(f"{line_where}: batch_id 缺失或为空")
            continue
        batch_id = batch_id.strip()
        if batch_id in local_seen:
            problems.append(f"{line_where}: 批 {batch_id} 在本操作 {op_id} 内重复列出")
            continue
        qty = _parse_qty({"mass": item.get("mass"), "unit": item.get("unit")},
                         line_where, problems)
        if qty is None:
            continue
        local_seen.add(batch_id)
        lines.append(MassLine(batch_id=batch_id, qty=qty, row=j))
    return lines


def load_operations(path: str | Path, problems: List[str]) -> List[Operation]:
    label = Path(path).name
    raw = _load_json(path, label, problems)
    if raw is None:
        return []
    unknown = set(raw) - {"operations"}
    if unknown:
        problems.append(f"{label}: 顶层含未知字段 {sorted(unknown)}")
    records = raw.get("operations")
    if not isinstance(records, list):
        problems.append(f"{label}: 顶层 operations 必须是数组")
        return []

    operations: List[Operation] = []
    seen_ids: Set[str] = set()
    for idx, item in enumerate(records, start=1):
        where = f"{label} 操作记录#{idx}"
        if not isinstance(item, dict):
            problems.append(f"{where}: 记录必须是对象")
            continue
        extra = set(item) - {"operation_id", "inputs", "outputs", "loss_mass"}
        if extra:
            problems.append(f"{where}: 含未知字段 {sorted(extra)}")
        op_id = item.get("operation_id")
        if not isinstance(op_id, str) or not op_id.strip():
            problems.append(f"{where}: operation_id 缺失或为空")
            continue
        op_id = op_id.strip()
        if op_id in seen_ids:
            problems.append(f"{where}: operation_id 重复: {op_id}")
            continue
        inputs = _parse_lines(item.get("inputs"), where, problems, op_id)
        outputs = _parse_lines(item.get("outputs"), where, problems, op_id)
        loss = _parse_qty(item.get("loss_mass"), f"{where} loss_mass",
                          problems, allow_zero=True)
        if inputs is None or outputs is None or loss is None:
            continue
        seen_ids.add(op_id)
        operations.append(Operation(operation_id=op_id, inputs=tuple(inputs),
                                    outputs=tuple(outputs), loss=loss, row=idx))

    # 单据级守恒：输入总量必须**恰好**等于产出总量 + 损耗（无容差）
    for op in operations:
        diff = op.input_total_g - op.output_total_g - op.loss.grams
        if diff != 0:
            problems.append(
                f"{label} 操作记录#{op.row}（{op.operation_id}）: 质量不守恒，"
                f"输入合计 {op.input_total_g} g ≠ 产出合计 {op.output_total_g} g "
                f"+ 损耗 {op.loss.grams} g，差额 {diff} g（精确核算，无容差）"
            )
        in_ids = {line.batch_id for line in op.inputs}
        out_ids = {line.batch_id for line in op.outputs}
        both = in_ids & out_ids
        if both:
            problems.append(
                f"{label} 操作记录#{op.row}（{op.operation_id}）: 批 {sorted(both)} "
                f"同时出现在输入与输出中，操作不能自耗自产"
            )
    return operations


# --------------------------------------------------------------------------- #
# 交叉校验：引用、唯一生产者、成环、累计超领
# --------------------------------------------------------------------------- #
def _cross_check(materials: List[RawBatch],
                 operations: List[Operation],
                 problems: List[str]) -> None:
    label_ops = "operations"
    raw_ids = {b.batch_id for b in materials}

    # 产出批：只能有一个生产者；不能覆盖外购批或另一项操作的产出
    producer: Dict[str, Operation] = {}
    for op in operations:
        for line in op.outputs:
            bid = line.batch_id
            if bid in raw_ids:
                problems.append(
                    f"{label_ops} 操作记录#{op.row}（{op.operation_id}）产出行#{line.row}: "
                    f"产出批 {bid} 与外购原料批同名，不允许覆盖"
                )
            elif bid in producer:
                first = producer[bid]
                problems.append(
                    f"{label_ops} 操作记录#{op.row}（{op.operation_id}）产出行#{line.row}: "
                    f"产出批 {bid} 已有生产者 {first.operation_id}（操作记录#{first.row}），"
                    f"一个产出批只能有一个生产者"
                )
            else:
                producer[bid] = op

    known = raw_ids | set(producer)
    # 引用不存在的批
    for op in operations:
        for line in op.inputs:
            if line.batch_id not in known:
                problems.append(
                    f"{label_ops} 操作记录#{op.row}（{op.operation_id}）输入行#{line.row}: "
                    f"引用了不存在的批 {line.batch_id}"
                )

    # 操作级依赖图：边 producer_op -> consumer_op（按被消费的中间批建立）
    edges: Dict[str, Set[str]] = {op.operation_id: set() for op in operations}
    for op in operations:
        for line in op.inputs:
            src = producer.get(line.batch_id)
            if src is not None and src.operation_id != op.operation_id:
                edges[src.operation_id].add(op.operation_id)

    # Tarjan 强连通分量：规模 >1 的 SCC 即循环依赖
    index_counter = [0]
    stack: List[str] = []
    on_stack: Set[str] = set()
    indices: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    sccs: List[List[str]] = []

    def strong_connect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(edges[v]):
            if w not in indices:
                strong_connect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            component: List[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            sccs.append(component)

    for op_id in sorted(edges):
        if op_id not in indices:
            strong_connect(op_id)
    op_by_id = {op.operation_id: op for op in operations}
    for component in sccs:
        if len(component) > 1:
            detail = ", ".join(
                f"{oid}（操作记录#{op_by_id[oid].row}）" for oid in sorted(component))
            problems.append(f"检测到循环依赖，成环操作: {detail}；"
                            f"加工顺序无法定义，拒绝本次装载")

    # 累计超领：与记录顺序无关，按全部操作汇总每个批的实耗
    available: Dict[str, Fraction] = {b.batch_id: b.initial.grams for b in materials}
    for bid, op in producer.items():
        produced = next(line.qty.grams for line in op.outputs
                        if line.batch_id == bid)
        available[bid] = produced
    consumed: Dict[str, Fraction] = {bid: Fraction(0) for bid in known}
    consumers: Dict[str, List[MassLine]] = {bid: [] for bid in known}
    for op in operations:
        for line in op.inputs:
            if line.batch_id in consumed:
                consumed[line.batch_id] += line.qty.grams
                consumers[line.batch_id].append(
                    MassLine(batch_id=op.operation_id, qty=line.qty, row=op.row))
    for bid in sorted(known):
        if consumed[bid] > available[bid]:
            trace = "; ".join(
                f"{line.batch_id}（操作记录#{line.row}）实耗 {line.qty.grams} g"
                for line in consumers[bid])
            problems.append(
                f"批 {bid} 累计实耗 {consumed[bid]} g 超过可用量 {available[bid]} g，"
                f"超领 {consumed[bid] - available[bid]} g（精确核算，无容差）；"
                f"领用明细: {trace}"
            )


def load_lineage_input(materials: str | Path,
                       operations: str | Path
                       ) -> Tuple[Tuple[RawBatch, ...], Tuple[Operation, ...]]:
    """读取并交叉校验两份输入；任何数据问题都使本次装载被整体拒绝。"""
    problems: List[str] = []
    raw_batches = load_materials(materials, problems)
    operations = load_operations(operations, problems)
    if not problems:  # 结构层通过后才做交叉校验，避免级联误报
        _cross_check(raw_batches, operations, problems)
    if problems:
        raise LineageInputError(problems)
    return tuple(raw_batches), tuple(operations)
