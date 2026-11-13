"""纯 Python 小型 MILP 求解器（不新增第三方依赖）。

构成：
1. 两阶段单纯形法（Bland 规则防循环）求解 LP 松弛；
2. 分支定界处理整桶整数约束与互斥约束；
3. 删除过滤（deletion filter）提取不可行冲突约束集（IIS）：
   该集合整体不可行，移除其中任意一条即恢复可行，可通过重新求解验证。

数值约定：
- 所有约束行在求解前按最大系数绝对值做行缩放；
- 可行性容差 1e-7，整数容差 1e-6；
- 分支定界有节点上限，超限返回 NODE_LIMIT，绝不把未收敛结果当作最优。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Sequence, Set, Tuple

_EPS = 1e-9
_FEAS_TOL = 1e-7
_INT_TOL = 1e-6
_MAX_SIMPLEX_ITER = 200_000
_DEFAULT_NODE_LIMIT = 100_000

# LP 状态
LP_OPTIMAL = "optimal"
LP_INFEASIBLE = "infeasible"
LP_UNBOUNDED = "unbounded"

# MILP 状态
MILP_OPTIMAL = "OPTIMAL"
MILP_INFEASIBLE = "INFEASIBLE"
MILP_NODE_LIMIT = "NODE_LIMIT"


# --------------------------------------------------------------------------- #
# 两阶段单纯形
# --------------------------------------------------------------------------- #
def _pivot(tab: List[List[float]], basis: List[int], row: int, col: int) -> None:
    piv = tab[row][col]
    inv = 1.0 / piv
    r = tab[row]
    for j in range(len(r)):
        r[j] *= inv
    for i in range(len(tab)):
        if i == row:
            continue
        factor = tab[i][col]
        if factor == 0.0:
            continue
        ri = tab[i]
        for j in range(len(ri)):
            ri[j] -= factor * r[j]
    basis[row] = col


def _reduced_costs(tab: List[List[float]], basis: List[int],
                   costs: Sequence[float], n_cols: int) -> List[float]:
    """rc_j = c_j - sum_i c_B[i] * tab[i][j]（每次迭代全量重算，问题规模小，可靠优先）。"""
    rc = list(costs[:n_cols])
    for i, b in enumerate(basis):
        cb = costs[b]
        if cb == 0.0:
            continue
        row = tab[i]
        for j in range(n_cols):
            if row[j] != 0.0:
                rc[j] -= cb * row[j]
    return rc


def _simplex_core(tab: List[List[float]], basis: List[int], costs: List[float],
                  forbidden: Set[int]) -> str:
    """在给定 tableau 上按 Bland 规则迭代到最优或无界。"""
    m = len(tab)
    n_cols = len(costs)
    for _ in range(_MAX_SIMPLEX_ITER):
        in_basis = set(basis)
        rc = _reduced_costs(tab, basis, costs, n_cols)
        entering = -1
        for j in range(n_cols):
            if j in in_basis or j in forbidden:
                continue
            if rc[j] < -_EPS:
                entering = j  # Bland：取最小下标
                break
        if entering < 0:
            return LP_OPTIMAL
        leaving = -1
        best_ratio = 0.0
        for i in range(m):
            a = tab[i][entering]
            if a > _EPS:
                ratio = tab[i][-1] / a
                if ratio < -_FEAS_TOL:
                    continue  # 数值噪声，忽略
                if leaving < 0 or ratio < best_ratio - _EPS or (
                    abs(ratio - best_ratio) <= _EPS and basis[i] < basis[leaving]
                ):
                    leaving = i
                    best_ratio = ratio
        if leaving < 0:
            return LP_UNBOUNDED
        _pivot(tab, basis, leaving, entering)
    raise RuntimeError("单纯形迭代超过上限，未收敛")


def solve_lp(c: Sequence[float], rows: Sequence[Tuple[Sequence[float], float, str]]
             ) -> Tuple[str, float, List[float]]:
    """求解 ``min c·x``，约束 ``rows`` 为 ``(系数, 右端, sense)``，sense ∈ {"<=", "="}，x >= 0。

    返回 ``(status, objective, x)``；不可行/无界时 objective 与 x 无意义。
    """
    n = len(c)
    # 行缩放 + 负右端翻号（"<=" 变 ">="，"=" 不变）
    norm_rows: List[Tuple[List[float], float, str]] = []
    for coeffs, rhs, sense in rows:
        coeffs = [float(v) for v in coeffs]
        rhs = float(rhs)
        if sense not in ("<=", "="):
            raise ValueError(f"不支持的约束方向: {sense!r}")
        if rhs < 0.0:
            coeffs = [-v for v in coeffs]
            rhs = -rhs
            sense = {"<=": ">=", "=": "="}[sense]
        scale = max((abs(v) for v in coeffs), default=0.0)
        if scale > 0.0:
            coeffs = [v / scale for v in coeffs]
            rhs /= scale
        norm_rows.append((coeffs, rhs, sense))

    # 组装标准形：结构变量 + 松弛 + 人工变量
    table_rows: List[List[float]] = []
    rhs_values: List[float] = []
    basis: List[int] = []
    artificial_cols: List[int] = []
    widths = n
    for coeffs, rhs, sense in norm_rows:
        row = list(coeffs)
        row += [0.0] * (widths - len(row))  # 对齐到当前列宽
        if sense == "<=":
            row += [1.0]  # 松弛变量，直接入基
            basis.append(widths)
            widths += 1
        else:
            if sense == ">=":
                row += [-1.0]  # 剩余变量
                widths += 1
            row += [1.0]  # 人工变量入基
            artificial_cols.append(widths)
            basis.append(widths)
            widths += 1
        table_rows.append(row)
        rhs_values.append(rhs)
    for i, row in enumerate(table_rows):
        row.extend([0.0] * (widths - len(row)))  # 先补齐系数列
        row.append(rhs_values[i])                # 右端永远在最后

    m = len(table_rows)
    if m == 0:
        if all(v >= -_EPS for v in c):
            return LP_OPTIMAL, 0.0, [0.0] * n
        return LP_UNBOUNDED, float("-inf"), [0.0] * n

    forbidden: Set[int] = set()
    if artificial_cols:
        # 阶段一：最小化人工变量之和
        c1 = [0.0] * widths
        for j in artificial_cols:
            c1[j] = 1.0
        status = _simplex_core(table_rows, basis, c1, forbidden)
        if status != LP_OPTIMAL:
            return LP_INFEASIBLE, 0.0, [0.0] * n
        phase1_obj = sum(table_rows[i][-1] for i in range(m)
                         if basis[i] in artificial_cols)
        if phase1_obj > _FEAS_TOL:
            return LP_INFEASIBLE, 0.0, [0.0] * n
        # 把仍在基中的人工变量（值必为 0）换出；换不出则该约束冗余，删行
        non_artificial = [j for j in range(widths) if j not in artificial_cols]
        keep: List[int] = []
        for i in range(m):
            if basis[i] not in artificial_cols:
                keep.append(i)
                continue
            for j in non_artificial:
                if abs(table_rows[i][j]) > _EPS:
                    _pivot(table_rows, basis, i, j)
                    keep.append(i)
                    break
        if len(keep) < m:
            table_rows = [table_rows[i] for i in keep]
            basis = [basis[i] for i in keep]
            m = len(keep)
        forbidden = set(artificial_cols)

    # 阶段二：原目标
    c2 = [0.0] * widths
    for j in range(n):
        c2[j] = float(c[j])
    status = _simplex_core(table_rows, basis, c2, forbidden)
    if status == LP_UNBOUNDED:
        return LP_UNBOUNDED, float("-inf"), [0.0] * n

    x = [0.0] * n
    for i in range(m):
        if basis[i] < n:
            x[basis[i]] = table_rows[i][-1]
    obj = sum(float(c[j]) * x[j] for j in range(n))
    return LP_OPTIMAL, obj, x


# --------------------------------------------------------------------------- #
# 命名约束模型与分支定界
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NamedRow:
    """一条可单独启停的命名约束行：``coeffs·x (sense) rhs``。"""

    cid: str
    coeffs: Tuple[float, ...]
    rhs: float
    sense: str  # "<=" 或 "="


@dataclass(frozen=True)
class MilpSpec:
    """配料 MILP 模型。

    - ``rows``          命名线性约束（库存上限、质量守恒、元素上下限等）；
    - ``integer_steps`` cid -> (变量下标, 步长)：整桶约束，x_i 必须为步长整数倍；
    - ``mutex_pairs``   cid -> (i, j)：互斥约束，两者至多一个非零。

    整数与互斥约束同样以 cid 命名，便于纳入冲突约束集。
    """

    objective: Tuple[float, ...]
    rows: Tuple[NamedRow, ...]
    integer_steps: Mapping[str, Tuple[int, float]] = field(default_factory=dict)
    mutex_pairs: Mapping[str, Tuple[int, int]] = field(default_factory=dict)

    @property
    def n_vars(self) -> int:
        return len(self.objective)

    def constraint_ids(self) -> List[str]:
        return ([r.cid for r in self.rows]
                + sorted(self.integer_steps) + sorted(self.mutex_pairs))


@dataclass(frozen=True)
class MilpOutcome:
    status: str  # MILP_OPTIMAL / MILP_INFEASIBLE / MILP_NODE_LIMIT
    objective: float
    x: Tuple[float, ...]
    nodes: int
    lp_solves: int


def _active_parts(spec: MilpSpec, active: Optional[Set[str]]):
    rows = [r for r in spec.rows if active is None or r.cid in active]
    ints = {cid: v for cid, v in spec.integer_steps.items()
            if active is None or cid in active}
    mutex = {cid: v for cid, v in spec.mutex_pairs.items()
             if active is None or cid in active}
    return rows, ints, mutex


def solve_milp(spec: MilpSpec, active: Optional[Set[str]] = None,
               node_limit: int = _DEFAULT_NODE_LIMIT) -> MilpOutcome:
    """分支定界求解。``active`` 给定后仅启用对应 cid 的约束（用于 IIS 提取）。"""
    rows, int_steps, mutex = _active_parts(spec, active)
    n = spec.n_vars
    base_rows = [(list(r.coeffs), r.rhs, r.sense) for r in rows]
    obj = list(spec.objective)
    step_of = {i: s for _, (i, s) in int_steps.items()}

    incumbent_obj = float("inf")
    incumbent_x: Optional[List[float]] = None
    nodes = 0
    lp_solves = 0
    # 栈中元素：该节点追加的约束行列表（x_i <= v 或 -x_i <= -v）
    stack: List[List[Tuple[List[float], float, str]]] = [[]]

    while stack:
        node_rows = stack.pop()
        nodes += 1
        if nodes > node_limit:
            return MilpOutcome(MILP_NODE_LIMIT, incumbent_obj,
                               tuple(incumbent_x) if incumbent_x else (),
                               nodes, lp_solves)
        lp_solves += 1
        status, lp_obj, x = solve_lp(obj, base_rows + node_rows)
        if status != LP_OPTIMAL:
            continue  # 不可行（本问题有质量守恒，不会无界）
        if lp_obj >= incumbent_obj - _FEAS_TOL:
            continue

        # 找最分数的整桶变量
        branch_var = -1
        branch_frac = 0.0
        integer_ok = True
        for i in sorted(step_of):
            step = step_of[i]
            r = x[i] / step
            frac = abs(r - round(r))
            if frac > _INT_TOL:
                integer_ok = False
                part = r - math.floor(r)
                dist = min(part, 1.0 - part)
                if dist > branch_frac:
                    branch_frac = dist
                    branch_var = i
        if integer_ok:
            # 检查互斥
            violated = None
            for cid in sorted(mutex):
                i, j = mutex[cid]
                if x[i] > _INT_TOL and x[j] > _INT_TOL:
                    violated = (i, j)
                    break
            if violated is None:
                incumbent_obj = lp_obj
                incumbent_x = list(x)
                continue
            i, j = violated
            row_i = [0.0] * n
            row_i[i] = 1.0
            row_j = [0.0] * n
            row_j[j] = 1.0
            stack.append(node_rows + [(row_j, 0.0, "<=")])  # x_j = 0
            stack.append(node_rows + [(row_i, 0.0, "<=")])  # x_i = 0
            continue

        # 整桶分支：x_i <= floor(r)*step 或 x_i >= ceil(r)*step
        i = branch_var
        step = step_of[i]
        r = x[i] / step
        down = [0.0] * n
        down[i] = 1.0
        up = [0.0] * n
        up[i] = -1.0
        stack.append(node_rows + [(up, -math.ceil(r - _INT_TOL) * step, "<=")])
        stack.append(node_rows + [(down, math.floor(r + _INT_TOL) * step, "<=")])

    if incumbent_x is not None:
        return MilpOutcome(MILP_OPTIMAL, incumbent_obj, tuple(incumbent_x),
                           nodes, lp_solves)
    return MilpOutcome(MILP_INFEASIBLE, float("inf"), (), nodes, lp_solves)


# --------------------------------------------------------------------------- #
# 冲突约束集（IIS）提取：删除过滤
# --------------------------------------------------------------------------- #
def extract_iis(spec: MilpSpec, node_limit: int = _DEFAULT_NODE_LIMIT) -> Tuple[str, ...]:
    """删除过滤求一个**极小**不可行约束集：整体不可行，去掉任意一条即可行。

    返回的集合可通过重新求解逐条验证（这正是交付物要求的验证方式）。
    """
    full = set(spec.constraint_ids())
    if solve_milp(spec, full, node_limit).status != MILP_INFEASIBLE:
        raise ValueError("模型并非不可行，无法提取冲突约束集")
    active = set(full)
    for cid in sorted(full):
        trial = active - {cid}
        if solve_milp(spec, trial, node_limit).status == MILP_INFEASIBLE:
            active = trial
    return tuple(sorted(active))
