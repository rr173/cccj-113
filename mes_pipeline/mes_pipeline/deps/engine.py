"""依赖发现与规则核查的核心引擎。

处理分三步，**任何一步都不构造全体行对**：

1. **一次扫描 → 列等值索引**（:class:`TableIndex`）：按 ``row_id`` 的 Unicode
   顺序重排记录（该顺序即反例取对的比较序），每列把值映射成紧凑整数码
   （``null`` 单独占一个码），并保留非空行的位图。原始字符串只保留一份。
2. **组合搜索**（:func:`discover`）：按左部大小逐层枚举（BFS），用列码拼接成
   组合标签（等值类 ID），逐候选精确核对「是否存在冲突」与「参与行数」。
   唯一键 = 组合标签互不相同；函数依赖 = 同左部标签内右部码唯一。
   搜索覆盖 ``1..k`` 的全部组合，不抽样、不依赖哈希碰撞的侥幸：冲突判定
   是逐行精确比较，哈希只用于分组。
3. **规则核查**（:func:`check_key` / :func:`check_fd`）：对任意指定规则逐行
   扫描，给出成立 / 违反 / 证据不足；违反时按 ``(row_id_a, row_id_b)``
   字典序取最小的一对实际反例行。

空值口径逐规则生效：

- ``equal``：``null`` 是与任何字符串都不同的共同值，所有行参与；
- ``ignore``：唯一键排除键列含 null 的行；X → A 排除 X 或 A 含 null 的行。

``min_rows`` 是证据门槛。引擎同时记录 ``conflict_free``（是否找到反例）与
``participating``（参与行数）：只有二者都满足才列为成立。无冲突但参与行数
不足的规则进入 ``evidence_gaps``，明确表示「没有反例，但资料不足以支持」。

规模上限（超出即 :class:`~mes_pipeline.deps.io.ScaleExceeded` 拒绝，绝不把
部分搜索标成完整）：列数 ≤ 32、行数 ≤ 200000、``1..k`` 的全部组合数 ≤ 200000、
单元格数 ≤ 20000000。
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from .io import ScaleExceeded, validate_search_params
from .models import (
    DEFAULT_MAX_COLUMNS,
    DEFAULT_MAX_MAX_SCAN_CELLS,
    DEFAULT_MAX_ROWS,
    NULL_EQUAL,
    NULL_IGNORE,
    STATUS_HOLDS,
    STATUS_INSUFFICIENT,
    STATUS_NOT_APPLICABLE,
    STATUS_VIOLATED,
    ColumnIndex,
    Dataset,
    DeleteSummary,
    Evidence,
    FunctionalDependency,
    UniqueKey,
    Violation,
)

# 组合搜索的默认上限：sum_{i=1..k} C(列数, i) 不得超过此值
DEFAULT_MAX_SUBSETS = 200_000

# 位图 → 行号元组的缓存容量上限（防止派生位图撑爆内存）
_MASK_CACHE_LIMIT = 128


class RuleError(ValueError):
    """规则核查参数不合法（列不存在、长度超 k、右部在左部内等）。"""

    status = "INVALID_INPUT"

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("规则不合法:\n" + "\n".join(f"- {p}" for p in self.problems))


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #
@dataclass
class TableIndex:
    """一次扫描得到的列等值索引（行按 row_id 的 Unicode 顺序排列）。

    ``codes[c][row]`` 是第 c 列在排序后第 row 行的值码；``null`` 用
    ``cards[c]``（即该列字符串取值个数）作为码。``masks[c]`` 的第 row 位
    为 1 表示该列非 null。原始字符串只在 ``value_names`` 中存一份。
    """

    table_id: str
    columns: Tuple[str, ...]
    row_ids: Tuple[str, ...]
    codes: Tuple[array, ...]
    cards: Tuple[int, ...]
    masks: Tuple[int, ...]
    value_names: Tuple[Tuple[str, ...], ...]
    null_counts: Tuple[int, ...]
    source: str = ""
    _mask_rows: Dict[int, Tuple[int, ...]] = field(default_factory=dict, repr=False)

    def rows_for(self, mask: int) -> Tuple[int, ...]:
        """位图 → 升序行号元组（小容量缓存，避免重复的逐位循环）。

        缓存上限固定，避免大量派生位图把内存撑爆；超过上限后只计算不缓存。
        """
        cached = self._mask_rows.get(mask)
        if cached is None:
            cached = tuple(_iter_rows(mask))
            if len(self._mask_rows) < _MASK_CACHE_LIMIT:
                self._mask_rows[mask] = cached
        return cached

    def labels_dense(self, cols: Sequence[int]) -> Tuple[List[int], int]:
        """把左部列的等值类压成稠密标签列表，返回 (labels, 类数)。"""
        cols = tuple(cols)
        if not cols:
            return [0] * self.row_count, 1
        if len(cols) == 1:
            c = cols[0]
            return self.codes[c].tolist(), self.cards[c] + 1
        code_lists = [self.codes[c].tolist() for c in cols]
        packed = [0] * self.row_count
        for c, values in zip(cols, code_lists):
            base = self.cards[c] + 1
            packed = [p * base + v for p, v in zip(packed, values)]
        dense: Dict[int, int] = {}
        labels = [0] * self.row_count
        for i, value in enumerate(packed):
            label = dense.get(value)
            if label is None:
                label = len(dense)
                dense[value] = label
            labels[i] = label
        return labels, len(dense)

    @property
    def row_count(self) -> int:
        return len(self.row_ids)

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def value_of(self, col: int, code: int) -> Optional[str]:
        """值码 → 原始字符串；``null`` 码返回 None（equal 口径下也会出现）。"""
        if code == self.cards[col]:
            return None
        return self.value_names[col][code]

    def values_at(self, row: int, cols: Sequence[int]) -> Tuple[Optional[str], ...]:
        return tuple(self.value_of(c, self.codes[c][row]) for c in cols)

    def column_names(self, cols: Sequence[int]) -> Tuple[str, ...]:
        return tuple(self.columns[c] for c in cols)

    def column_indexes(self) -> Tuple[ColumnIndex, ...]:
        """按「值 → 升序行号」生成列分组（供解释与键核查使用）。"""
        out: List[ColumnIndex] = []
        for c in range(self.column_count):
            codes = self.codes[c]
            null_code = self.cards[c]
            buckets: List[List[int]] = [[] for _ in range(null_code + 1)]
            for row in range(self.row_count):
                buckets[codes[row]].append(row)
            rows: List[int] = []
            starts: List[int] = []
            values: List[Optional[str]] = []
            for code in range(null_code + 1):
                group = buckets[code]
                if not group:  # 该值在本列未出现（null 可能不存在）
                    continue
                starts.append(len(rows))
                values.append(None if code == null_code else self.value_names[c][code])
                rows.extend(group)
            out.append(ColumnIndex(column=c, rows=tuple(rows),
                                   run_starts=tuple(starts),
                                   run_values=tuple(values),
                                   null_count=self.null_counts[c]))
        return tuple(out)


def build_index(dataset: Dataset, *, max_columns: int = DEFAULT_MAX_COLUMNS,
                max_rows: int = DEFAULT_MAX_ROWS,
                max_cells: int = DEFAULT_MAX_MAX_SCAN_CELLS) -> TableIndex:
    """把已校验资料转成列等值索引；规模超限在开始前拒绝。"""
    problems: List[str] = []
    if dataset.column_count > max_columns:
        problems.append(f"列数 {dataset.column_count} 超过声明的上限 {max_columns}")
    if dataset.row_count > max_rows:
        problems.append(f"行数 {dataset.row_count} 超过声明的上限 {max_rows}")
    if dataset.column_count * dataset.row_count > max_cells:
        problems.append(f"单元格数 {dataset.column_count * dataset.row_count} "
                        f"超过声明的上限 {max_cells}")
    if problems:
        raise ScaleExceeded(problems)

    order = sorted(range(dataset.row_count), key=dataset.row_ids.__getitem__)
    row_ids = tuple(dataset.row_ids[r] for r in order)
    columns = dataset.columns
    codes_out: List[array] = []
    cards: List[int] = []
    masks: List[int] = []
    names: List[Tuple[str, ...]] = []
    null_counts: List[int] = []

    for c in range(dataset.column_count):
        present = sorted({dataset.values[r][c] for r in range(dataset.row_count)
                          if dataset.values[r][c] is not None})
        id_of = {value: i for i, value in enumerate(present)}
        null_code = len(present)
        codes = array("i", bytes(4 * dataset.row_count))
        mask = 0
        null_count = 0
        for position, r in enumerate(order):
            value = dataset.values[r][c]
            if value is None:
                codes[position] = null_code
                null_count += 1
            else:
                codes[position] = id_of[value]
                mask |= 1 << position
        codes_out.append(codes)
        cards.append(null_code)
        masks.append(mask)
        names.append(tuple(present))
        null_counts.append(null_count)

    return TableIndex(
        table_id=dataset.table_id,
        columns=columns,
        row_ids=row_ids,
        codes=tuple(codes_out),
        cards=tuple(cards),
        masks=tuple(masks),
        value_names=tuple(names),
        null_counts=tuple(null_counts),
        source=dataset.source,
    )


# --------------------------------------------------------------------------- #
# 逐行核查（发现结果与规则核查共用；不读文件、不建行对）
# --------------------------------------------------------------------------- #
def _iter_rows(mask: int) -> Iterator[int]:
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


def _left_mask(index: TableIndex, cols: Sequence[int], policy: str) -> int:
    all_rows = (1 << index.row_count) - 1
    if policy == NULL_EQUAL:
        return all_rows
    mask = all_rows
    for c in cols:
        mask &= index.masks[c]
    return mask


def _make_evidence(index: TableIndex, participating: int, excluded_left: int,
                   excluded_right: int, min_rows: int, status: str,
                   policy: str) -> Evidence:
    return Evidence(
        participating=participating,
        row_total=index.row_count,
        excluded_null_left=excluded_left,
        excluded_null_right=excluded_right,
        excluded_other=0,
        min_rows=min_rows,
        status=status,
        null_policy=policy,
    )


def _status(conflict: bool, participating: int, min_rows: int) -> str:
    if conflict:
        return STATUS_VIOLATED
    if participating < min_rows:
        return STATUS_INSUFFICIENT
    return STATUS_HOLDS


def _violation_pair(index: TableIndex, a: int, b: int, left_cols: Sequence[int],
                    right: Optional[int] = None,
                    right_codes: Optional[Tuple[int, int]] = None) -> Violation:
    left_a = index.values_at(a, left_cols)
    left_b = index.values_at(b, left_cols)
    violation = Violation(row_id_a=index.row_ids[a], row_id_b=index.row_ids[b],
                          left_a=left_a, left_b=left_b)
    if right is not None and right_codes is not None:
        violation = Violation(
            row_id_a=index.row_ids[a], row_id_b=index.row_ids[b],
            left_a=left_a, left_b=left_b,
            right_a=index.value_of(right, right_codes[0]),
            right_b=index.value_of(right, right_codes[1]),
        )
    return violation


def check_key(index: TableIndex, cols: Sequence[int], policy: str, min_rows: int,
              *, need_counterexample: bool = True) -> Tuple[Evidence, Optional[Violation]]:
    """核查唯一键 K；返回证据摘要与反例（违反时按 row_id 字典序最小的一对）。"""
    cols = tuple(cols)
    mask = _left_mask(index, cols, policy)
    participating = mask.bit_count()
    excluded_left = index.row_count - participating if policy == NULL_IGNORE else 0
    labels, _groups = index.labels_dense(cols)
    rows = index.rows_for(mask)
    full = participating == index.row_count
    if not full:
        labels = [labels[row] for row in rows]

    first: Dict[int, int] = {}
    best: Optional[Tuple[int, int]] = None
    complete = need_counterexample
    if full:
        iterator = enumerate(labels)
    else:
        iterator = zip(rows, labels)
    for row, label in iterator:
        other = first.get(label)
        if other is None:
            first[label] = row
            continue
        pair = (other, row)
        if best is None or pair < best:
            best = pair
        if not complete:
            break

    conflict = best is not None
    status = _status(conflict, participating, min_rows)
    evidence = _make_evidence(index, participating, excluded_left, 0, min_rows,
                              status, policy)
    violation = None
    if best is not None and need_counterexample:
        violation = _violation_pair(index, best[0], best[1], cols)
    return evidence, violation


def check_fd(index: TableIndex, left: Sequence[int], right: int, policy: str,
             min_rows: int,
             *, need_counterexample: bool = True) -> Tuple[Evidence, Optional[Violation]]:
    """核查 X → A；返回证据摘要与反例（违反时按 row_id 字典序最小的一对）。"""
    left = tuple(left)
    if right in left:
        raise RuleError([f"右部列 {index.columns[right]!r} 不能同时出现在左部"])
    if not (0 <= right < index.column_count):
        raise RuleError([f"右部列序号 {right} 越界"])

    row_total = index.row_count
    mask = _left_mask(index, left, policy)
    excluded_left = row_total - mask.bit_count() if policy == NULL_IGNORE else 0
    if policy == NULL_IGNORE:
        mask &= index.masks[right]
    participating = mask.bit_count()
    excluded_right = (row_total - excluded_left - participating) if policy == NULL_IGNORE else 0

    right_codes = index.codes[right].tolist()
    labels, _groups = index.labels_dense(left)
    rows = index.rows_for(mask)
    full = participating == row_total
    if not full:
        labels = [labels[row] for row in rows]
        right_codes = [right_codes[row] for row in rows]

    first_row: Dict[int, int] = {}
    first_code: Dict[int, int] = {}
    best: Optional[Tuple[int, int]] = None
    best_codes: Optional[Tuple[int, int]] = None
    complete = need_counterexample
    if full:
        iterator = enumerate(zip(labels, right_codes))
    else:
        iterator = zip(rows, zip(labels, right_codes))
    for row, (label, code) in iterator:
        if label not in first_row:
            first_row[label] = row
            first_code[label] = code
            continue
        if code == first_code[label]:
            continue
        pair = (first_row[label], row)
        if best is None or pair < best:
            best, best_codes = pair, (first_code[label], code)
        if not complete:
            break

    conflict = best is not None
    status = _status(conflict, participating, min_rows)
    evidence = _make_evidence(index, participating, excluded_left, excluded_right,
                              min_rows, status, policy)
    violation = None
    if best is not None and best_codes is not None and need_counterexample:
        violation = _violation_pair(index, best[0], best[1], left, right, best_codes)
    return evidence, violation


@dataclass
class _KeyState:
    unique: bool
    participating: int
    holds: bool
    excluded_left: int


@dataclass
class _FdState:
    """一条候选依赖的状态；``conflict_free`` 为 None 表示「草拟待核实」。"""

    conflict_free: Optional[bool]
    participating: int
    holds: bool
    excluded_left: int
    excluded_right: int
    draft: bool = False


@dataclass
class _Level:
    labels: array          # 行 → 组合标签（不参与行为 0，但从不被迭代）
    mask: int              # 参与行位图
    groups: int            # 参与行内的等值类个数


@dataclass
class DiscoveryResult:
    table_id: str
    columns: Tuple[str, ...]
    row_ids: Tuple[str, ...]
    k: int
    min_rows: int
    null_policy: str
    total_rows: int
    keys: List[UniqueKey] = field(default_factory=list)
    fds: List[FunctionalDependency] = field(default_factory=list)
    evidence_gaps: List[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    claimable: bool = True
    complete: bool = True

    def as_dict(self) -> dict:
        return {
            "table_id": self.table_id,
            "columns": list(self.columns),
            "row_count": self.total_rows,
            "k": self.k,
            "min_rows": self.min_rows,
            "null_policy": self.null_policy,
            "complete": self.complete,
            "claimable": self.claimable,
            "unique_keys": [k_.as_dict() for k_ in self.keys],
            "functional_dependencies": [f.as_dict() for f in self.fds],
            "evidence_gaps": list(self.evidence_gaps),
            "stats": dict(self.stats),
            "notes": list(self.notes),
        }


def _comb(n: int, r: int) -> int:
    if r < 0 or r > n:
        return 0
    result = 1
    for i in range(1, r + 1):
        result = result * (n - r + i) // i
    return result


def _check_subset_limit(m: int, k: int, max_subsets: int) -> int:
    total = 0
    for size in range(1, k + 1):
        total += _comb(m, size)
        if total > max_subsets:
            raise ScaleExceeded(
                [f"k={k} 时 1..k 的全部列组合数为 {total}（超过声明上限 {max_subsets}）；"
                 f"请减小 k 或减少列数"])
    return total


def _constant_states(index: TableIndex, policy: str,
                     min_rows: int) -> Dict[Tuple[tuple, int], _FdState]:
    """空左部依赖（常量列）：逐列一次精确扫描。"""
    out: Dict[Tuple[tuple, int], _FdState] = {}
    for col in range(index.column_count):
        evidence, _ = check_fd(index, (), col, policy, min_rows,
                               need_counterexample=False)
        out[((), col)] = _FdState(
            conflict_free=evidence.status != STATUS_VIOLATED,
            participating=evidence.participating,
            holds=evidence.status == STATUS_HOLDS,
            excluded_left=evidence.excluded_null_left,
            excluded_right=evidence.excluded_null_right,
        )
    return out


def _singleton_level(index: TableIndex, col: int, policy: str) -> _Level:
    codes = index.codes[col]
    mask = (1 << index.row_count) - 1 if policy == NULL_EQUAL else index.masks[col]
    if policy == NULL_EQUAL:
        labels = array("i", codes)
        groups = len({codes[row] for row in range(index.row_count)})
    else:
        labels = array("i", bytes(4 * index.row_count))
        groups = 0
        seen = set()
        for row in _iter_rows(mask):
            label = codes[row]
            labels[row] = label
            seen.add(label)
        groups = len(seen)
    return _Level(labels=labels, mask=mask, groups=groups)


def _compose_level(index: TableIndex, parent: _Level, col: int,
                   policy: str) -> _Level:
    """把父层等值类标签与第 col 列的值码合并成新的等值类标签（稠密重编号）。

    重编号让标签始终落在 ``0..n-1``，既避免整数溢出，也让后续按行扫描
    的字典命中更集中；合并本身与行序无关。
    """
    base = index.cards[col] + 1
    codes = index.codes[col]
    p_labels = parent.labels
    mask = parent.mask
    if policy == NULL_IGNORE:
        mask &= index.masks[col]
    labels = array("i", bytes(4 * index.row_count))
    dense: Dict[int, int] = {}
    rows_iter: Iterator[int] = range(index.row_count) if policy == NULL_EQUAL else _iter_rows(mask)
    out = labels
    for row in rows_iter:
        key = p_labels[row] * base + codes[row]
        label = dense.get(key)
        if label is None:
            label = len(dense)
            dense[key] = label
        out[row] = label
    return _Level(labels=out, mask=mask, groups=len(dense))


def _scan_fd_conflict(index: TableIndex, level: _Level, right: int, policy: str,
                      restrict_mask: Optional[int] = None) -> bool:
    """逐行判定同左部标签内右部码是否唯一（精确比较，非抽样，凑到冲突即停）。

    预先把第 right 列与左部标签转为 Python 列表（各一次 C 级转换），再用
    ``zip`` 顺序扫描；标签是稠密编号，用列表下标代替字典查找。
    """
    codes = index.codes[right].tolist()
    labels = level.labels.tolist()
    null_code = index.cards[right]
    seen: List[int] = [-1] * (level.groups if level.groups else 1)
    if restrict_mask is None and policy == NULL_EQUAL:
        for label, code in zip(labels, codes):
            prev = seen[label]
            if prev < 0:
                seen[label] = code
            elif prev != code:
                return True
        return False
    mask = level.mask if restrict_mask is None else (level.mask & restrict_mask)
    ignore_null = policy == NULL_IGNORE
    for row in index.rows_for(mask):
        code = codes[row]
        if ignore_null and code == null_code:
            continue
        label = labels[row]
        prev = seen[label]
        if prev < 0:
            seen[label] = code
        elif prev != code:
            return True
    return False


def _restricted_groups(index: TableIndex, level: _Level, mask: int) -> int:
    labels = level.labels
    seen = set()
    for row in index.rows_for(mask):
        seen.add(labels[row])
    return len(seen)


# 依赖状态可分三种：True（已核实无冲突）/ False（有反例）/ None（草拟待核实）。
DRAFT = None


def _materialize_fd(index: TableIndex, levels: Dict[tuple, _Level], subset: tuple,
                    right: int, policy: str, min_rows: int, state: _FdState,
                    stats: dict) -> _FdState:
    """把一条草拟依赖在口径参与行上精确核实（逐行，凑到冲突即停）。

    只有需要判定最小性/成立性的候选才会走到这里；核实是精确的整数比较，
    不是抽样，也不依赖哈希碰撞。核实后 ``draft`` 置 False，结论可复用。
    """
    if not state.draft:
        return state
    lvl = levels[subset]
    restrict = index.masks[right] if policy == NULL_IGNORE else None
    conflict = _scan_fd_conflict(index, lvl, right, policy, restrict)
    stats["fd_scans"] += 1
    return _FdState(conflict_free=not conflict,
                    participating=state.participating,
                    holds=(not conflict) and state.participating >= min_rows,
                    excluded_left=state.excluded_left,
                    excluded_right=state.excluded_right,
                    draft=False)


def _level_states(index: TableIndex, levels: Dict[tuple, _Level], policy: str,
                  min_rows: int, key_state: Dict[tuple, _KeyState],
                  fd_state: Dict[Tuple[tuple, int], _FdState],
                  stats: dict) -> None:
    """记录每个左部层级的键状态与依赖的候选状态。

    唯一键：组合标签数 == 参与行数即为键，逐行精确、无需再核查。
    函数依赖：``groups(X ∪ {A}) == groups(X)`` 是「无冲突」的必要条件，
    据此可以**精确剔除**有冲突的候选（不存在因哈希碰撞而误判成立的风险）；
    但组合层计数是按各层自身参与行算的，ignore 且右部含 null 时必须按
    口径参与行重算左部组数才等价。为避免对每个候选都整列扫描，这里只把
    通过必要条件者标为「草拟（DRAFT）」，由 :func:`_materialize_fd` 在确定
    最小性时一次性精确核实——草稿不会被当作成立结果输出。
    """
    row_total = index.row_count
    masks = index.masks
    for subset, lvl in levels.items():
        participating = lvl.mask.bit_count()
        excluded_left = row_total - participating if policy == NULL_IGNORE else 0
        unique = lvl.groups == participating
        key_state[subset] = _KeyState(unique=unique, participating=participating,
                                      holds=unique and participating >= min_rows,
                                      excluded_left=excluded_left)
        for right in range(index.column_count):
            if right in subset:
                continue
            if policy == NULL_IGNORE:
                part = (lvl.mask & masks[right]).bit_count()
                ex_right = row_total - excluded_left - part
            else:
                part = row_total
                ex_right = 0
            if unique:
                conflict = False
            else:
                combined = tuple(sorted(set(subset) | {right}))
                if combined not in levels:
                    # 左部已是 k 列：X∪{A} 超出搜索范围，逐行精确扫描。
                    conflict = _scan_fd_conflict(index, lvl, right, policy)
                    stats["fd_scans"] += 1
                elif policy == NULL_IGNORE:
                    # 口径参与行是 X∪{A} 这一组合层的参与行；在其上：
                    #   groups(X∪{A}) == 参与行数  ⇒ 必无冲突（X 上同样两两不同）
                    #   groups(X∪{A}) <  参与行数  ⇒ 必有两行在 X∪{A} 上同值，
                    #                               也就同在 X 上同值，必冲突
                    # 只有「X 侧还有合并余地」时计数不足以定论，需逐行核实。
                    ref = _restricted_groups(index, lvl, lvl.mask & masks[right])
                    upper = _restricted_groups(index, lvl, lvl.mask & masks[right])
                    if levels[combined].groups == part:
                        conflict = False
                        stats["fd_upper_bound_checks"] += 1
                    elif levels[combined].groups == ref and ref == upper:
                        conflict = False
                        stats["fd_upper_bound_checks"] += 1
                    elif levels[combined].groups == ref:
                        conflict = True
                        stats["fd_count_checks"] += 1
                    else:
                        conflict = True
                        stats["fd_upper_bound_checks"] += 1
                else:
                    # equal 口径：全部行都参与，X∪{A} 层与 X 层的行集相同，
                    # 计数相等 ⇔ 无冲突，这是等价的精确判定。
                    conflict = levels[combined].groups != lvl.groups
                    stats["fd_count_checks"] += 1
            if conflict:
                fd_state[(subset, right)] = _FdState(
                    conflict_free=False, participating=part, holds=False,
                    excluded_left=excluded_left, excluded_right=ex_right)
            elif unique:
                fd_state[(subset, right)] = _FdState(
                    conflict_free=True, participating=part,
                    holds=part >= min_rows,
                    excluded_left=excluded_left, excluded_right=ex_right)
            else:
                # 计数一致只是「无冲突」的必要条件，尚不能下结论；草拟状态
                # 交由 _finalize 在需要判定最小性/成立性时精确核实一次。
                fd_state[(subset, right)] = _FdState(
                    conflict_free=None, participating=part, holds=False,
                    excluded_left=excluded_left, excluded_right=ex_right,
                    draft=True)


def _delete_key_summary(index: TableIndex, subset: tuple, col: int,
                        policy: str, min_rows: int) -> DeleteSummary:
    remaining = tuple(c for c in subset if c != col)
    if remaining:
        ev, viol = check_key(index, remaining, policy, min_rows)
        return DeleteSummary(
            deleted_column=index.columns[col],
            remaining_columns=index.column_names(remaining),
            evidence=ev, violation=viol)
    # 单列键：删到空组合。空组合不是唯一键（定义要求非空），这里不给出
    # 键反例——两行在空键上的值元组都为空、并不「相同」，用空键反例冒充
    # 「唯一键不成立」会误导。摘要如实标注「不适用」，而不是伪造一个核查结论。
    ev = Evidence(
        participating=index.row_count,
        row_total=index.row_count,
        excluded_null_left=0,
        excluded_null_right=0,
        excluded_other=0,
        min_rows=min_rows,
        status=STATUS_NOT_APPLICABLE,
        null_policy=policy,
    )
    return DeleteSummary(
        deleted_column=index.columns[col],
        remaining_columns=(),
        evidence=ev,
        violation=None,
        note="删去唯一列后只剩空组合；唯一键定义要求非空，空组合不是候选键，"
             "因此这里没有更小组合可核查。",
    )


def _delete_fd_summary(index: TableIndex, subset: tuple, right: int, col: int,
                       policy: str, min_rows: int) -> DeleteSummary:
    remaining = tuple(c for c in subset if c != col)
    ev, viol = check_fd(index, remaining, right, policy, min_rows)
    return DeleteSummary(
        deleted_column=index.columns[col],
        remaining_columns=index.column_names(remaining),
        evidence=ev, violation=viol,
    )


def _finalize(index: TableIndex, result: DiscoveryResult, k: int, min_rows: int,
              policy: str, key_state: Dict[tuple, _KeyState],
              fd_state: Dict[Tuple[tuple, int], _FdState],
              levels: Dict[tuple, _Level],
              *, empty_left_scope: bool = False) -> None:
    stats = result.stats

    def resolved(subset: tuple, right: int) -> Optional[_FdState]:
        """取（必要时先精确核实）一条依赖的状态。"""
        state = fd_state.get((subset, right))
        if state is None:
            return None
        if state.draft:
            state = _materialize_fd(index, levels, subset, right, policy, min_rows,
                                    state, stats)
            fd_state[(subset, right)] = state
        return state

    # ---- 最小唯一键：非空、成立，且不存在更小的成立子集 -------------------
    for subset in sorted(key_state, key=lambda s: (len(s), s)):
        state = key_state[subset]
        if not subset or not state.holds:
            continue
        if any(key_state[sub].holds
               for size in range(1, len(subset))
               for sub in combinations(subset, size)):
            continue
        evidence = _make_evidence(index, state.participating, state.excluded_left,
                                  0, min_rows, STATUS_HOLDS, policy)
        summaries = tuple(_delete_key_summary(index, subset, col, policy, min_rows)
                          for col in subset)
        result.keys.append(UniqueKey(
            columns=index.column_names(subset),
            evidence=evidence,
            delete_summaries=summaries,
        ))

    # ---- 最小函数依赖：左部可为空，且不存在更小的成立子集 ----------------
    # 按左部大小升序处理：更小左部一旦成立，更大左部就不可能是最小依赖，
    # 无需再核实其草拟状态（这只减少核查，不改变结论）。
    for (subset, right) in sorted(fd_state, key=lambda x: (len(x[0]), x[1], x[0])):
        blocked = any(
            (fd_state.get((sub, right)) is not None)
            and fd_state[(sub, right)].holds
            for size in range(0, len(subset))
            for sub in combinations(subset, size)
        )
        if blocked:
            continue
        state = resolved(subset, right)
        if state is None or not state.holds:
            continue
        evidence = _make_evidence(index, state.participating, state.excluded_left,
                                  state.excluded_right, min_rows, STATUS_HOLDS, policy)
        summaries = tuple(_delete_fd_summary(index, subset, right, col, policy, min_rows)
                          for col in subset)
        result.fds.append(FunctionalDependency(
            left=index.column_names(subset),
            right=index.columns[right],
            evidence=evidence,
            delete_summaries=summaries,
        ))

    # ---- 证据缺口：无冲突但参与行数不足（且没有更小的无冲突组合） --------
    lower_bound = 0 if empty_left_scope else 1
    for (subset, right) in sorted(fd_state, key=lambda x: (len(x[0]), x[1], x[0])):
        state = fd_state[(subset, right)]
        if state.holds or state.conflict_free is False:
            continue  # 已列为成立，或已有反例：都不是证据缺口
        resolved_state = resolved(subset, right)
        if not resolved_state.conflict_free or resolved_state.holds:
            continue
        if any((resolved(sub, right) is not None) and resolved(sub, right).conflict_free
               for size in range(lower_bound, len(subset))
               for sub in combinations(subset, size)):
            continue
        ev = _make_evidence(index, resolved_state.participating,
                            resolved_state.excluded_left, resolved_state.excluded_right,
                            min_rows, STATUS_INSUFFICIENT, policy)
        result.evidence_gaps.append({
            "kind": "functional_dependency",
            "left": list(result.columns[c] for c in subset),
            "right": result.columns[right],
            "conflict_free": True,
            "check": ev.as_dict(),
            "reason": "conflict_free_but_participating_below_min_rows",
        })
    for subset in sorted(key_state, key=lambda s: (len(s), s)):
        state = key_state[subset]
        # 只列「当前 k 范围内没有更小候选可解释」的缺口：单列键没有更小的
        # 非空键可试，因此单列的「无冲突但参与不足」正是最简洁的证据缺口。
        if not subset or not state.unique or state.holds:
            continue
        if any(key_state[sub].unique
               for size in range(1, len(subset))
               for sub in combinations(subset, size)):
            continue
        ev = _make_evidence(index, state.participating, state.excluded_left, 0,
                            min_rows, STATUS_INSUFFICIENT, policy)
        result.evidence_gaps.append({
            "kind": "unique_key",
            "columns": list(result.columns[c] for c in subset),
            "conflict_free": True,
            "check": ev.as_dict(),
            "reason": "conflict_free_but_participating_below_min_rows",
        })

    result.notes.append(
        "成立仅指在本资料、当前空值口径与 min_rows 下证据充足且未发现反例；"
        "不是供应商的永久承诺。")
    result.notes.append(
        f"本次搜索覆盖全部 {result.stats.get('subsets_in_scope', 0)} 个大小 1..{k} 的列组合，"
        f"未抽样、未截断。")


def discover(index: TableIndex, k: int, min_rows: int, policy: str,
             *, max_subsets: int = DEFAULT_MAX_SUBSETS,
             empty_left_scope: bool = False) -> DiscoveryResult:
    """在 ``1..k`` 的全部列组合上求最小唯一键与最小函数依赖。

    空左部依赖（``∅ → A``，表示常量列）在函数依赖定义中合法，因此始终被
    计算；``empty_left_scope`` 只影响 ``evidence_gaps`` 是否把「空左部」也
    作为「更小的无冲突候选」参与缺口最小性判断（供独立枚举器对照）。
    """
    k, min_rows = validate_search_params(k, min_rows, index.column_count)
    if policy not in (NULL_EQUAL, NULL_IGNORE):
        raise RuleError([f"未知空值口径 {policy!r}（应为 equal/ignore）"])

    subset_total = _check_subset_limit(index.column_count, k, max_subsets)
    result = DiscoveryResult(
        table_id=index.table_id,
        columns=index.columns,
        row_ids=index.row_ids,
        k=k,
        min_rows=min_rows,
        null_policy=policy,
        total_rows=index.row_count,
        stats={"subsets_in_scope": subset_total,
               "subsets_enumerated": 0,
               "fd_candidates": 0,
               "fd_count_checks": 0,
               "fd_upper_bound_checks": 0,
               "fd_scans": 0,
               "fd_restricted_counts": 0},
    )

    if index.row_count < min_rows:
        result.claimable = False
        result.notes.append(
            f"资料仅 {index.row_count} 行，少于 min_rows={min_rows}；"
            "任何规则都谈不上「证据充足」，因此不宣称发现键或依赖，"
            "也不把空结果解释为「不存在键/依赖」。表格仍会计算证据缺口。")

    levels: Dict[tuple, _Level] = {(c,): _singleton_level(index, c, policy)
                                   for c in range(index.column_count)}
    for size in range(2, k + 1):
        for subset in combinations(range(index.column_count), size):
            levels[subset] = _compose_level(index, levels[subset[:-1]],
                                            subset[-1], policy)

    key_state: Dict[tuple, _KeyState] = {}
    fd_state: Dict[Tuple[tuple, int], _FdState] = _constant_states(index, policy, min_rows)
    _level_states(index, levels, policy, min_rows, key_state, fd_state, result.stats)

    result.stats["subsets_enumerated"] = len(key_state)
    result.stats["fd_candidates"] = len(fd_state)
    _finalize(index, result, k, min_rows, policy, key_state, fd_state, levels,
              empty_left_scope=empty_left_scope)
    result.stats["fd_drafts_resolved"] = sum(
        1 for s in fd_state.values() if s.conflict_free is not None)
    del levels  # 组合层不再需要，尽早释放
    return result


# --------------------------------------------------------------------------- #
# 面向名称的公开入口（核查一条拟定规则）
# --------------------------------------------------------------------------- #
def resolve_columns(index: TableIndex, names: Sequence[str]) -> List[int]:
    """把列名解析成列序号；未知列/重复列/空规则给出问题清单。"""
    problems: List[str] = []
    positions: List[int] = []
    for name in names:
        if not isinstance(name, str):
            problems.append(f"列名必须是字符串，得到 {name!r}")
            continue
        try:
            position = index.columns.index(name)
        except ValueError:
            problems.append(f"列 {name!r} 不存在（现有列: {list(index.columns)}）")
            continue
        if position in positions:
            problems.append(f"列 {name!r} 重复出现在规则中")
            continue
        positions.append(position)
    if problems:
        raise RuleError(problems)
    return positions


def check_key_by_name(index: TableIndex, columns: Sequence[str], k: int, policy: str,
                      min_rows: int) -> dict:
    """核查任意长度 ≤ k 的唯一键，返回含结论/证据/反例的字典。"""
    k, min_rows = validate_search_params(k, min_rows, index.column_count)
    if policy not in (NULL_EQUAL, NULL_IGNORE):
        raise RuleError([f"未知空值口径 {policy!r}（应为 equal/ignore）"])
    if not columns:
        raise RuleError(["唯一键必须是非空列组合"])
    if len(columns) > k:
        raise RuleError([f"键长度 {len(columns)} 超过 k={k}"])
    positions = resolve_columns(index, columns)
    evidence, violation = check_key(index, positions, policy, min_rows)
    out = {
        "rule": {"kind": "unique_key", "columns": list(columns)},
        "k": k,
        "min_rows": min_rows,
        "verdict": evidence.status,
        "check": evidence.as_dict(),
    }
    if violation is not None:
        out["counterexample"] = violation.as_dict(columns)
    else:
        out["counterexample"] = None
    return out


def check_fd_by_name(index: TableIndex, left: Sequence[str], right: str, k: int,
                     policy: str, min_rows: int) -> dict:
    """核查任意单右部依赖 X → A（左部长度 ≤ k，允许为空）。"""
    k, min_rows = validate_search_params(k, min_rows, index.column_count)
    if policy not in (NULL_EQUAL, NULL_IGNORE):
        raise RuleError([f"未知空值口径 {policy!r}（应为 equal/ignore）"])
    if len(left) > k:
        raise RuleError([f"左部长度 {len(left)} 超过 k={k}"])
    positions = resolve_columns(index, left)
    right_position = resolve_columns(index, [right])[0]
    if right_position in positions:
        raise RuleError([f"右部列 {right!r} 不能同时出现在左部"])
    evidence, violation = check_fd(index, positions, right_position, policy, min_rows)
    out = {
        "rule": {"kind": "functional_dependency",
                 "left": list(left), "right": right,
                 "expression": "{} → {}".format(
                     "∅" if not left else ", ".join(left), right)},
        "k": k,
        "min_rows": min_rows,
        "verdict": evidence.status,
        "check": evidence.as_dict(),
    }
    if violation is not None:
        out["counterexample"] = violation.as_dict(left, right)
    else:
        out["counterexample"] = None
    return out
