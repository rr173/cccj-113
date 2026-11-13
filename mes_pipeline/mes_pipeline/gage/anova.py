"""平衡交叉设计的双因素随机效应 ANOVA（带零件×操作员交互）与 GRR 指标。

模型：``Y_ijk = μ + P_i + O_j + (PO)_ij + ε_ijk``，零件/操作员/交互/误差全部
随机效应，仅适用于完整平衡交叉设计（a 零件 × b 操作员 × 每格 n 次重复）。

平方和分解（N = a·b·n）：

- SS_P  = b·n·Σ(ȳ_i.. − ȳ)²，        df = a−1
- SS_O  = a·n·Σ(ȳ_j. − ȳ)²，         df = b−1
- SS_PO = n·Σ(ȳ_ij − ȳ_i.. − ȳ_j. + ȳ)²，df = (a−1)(b−1)
- SS_E  = Σ(y_ijk − ȳ_ij)²，          df = a·b·(n−1)
- SS_T  = Σ(y_ijk − ȳ)² = 四项之和，  df = N−1

交互检验：F = MS_PO / MS_E，p 用 F 分布生存函数（SciPy）。``p < alpha``
保留交互，否则把交互的平方和与自由度合并进误差后重新估计方差分量。
零分母规则（不产出 NaN 后继续比较）：

- MS_E = 0 且 MS_PO = 0：0/0 不可判定 → 保守**保留**交互；
- MS_E = 0 且 MS_PO > 0：F 发散 → 按**显著交互**处理（p 记 0），保留交互。

方差分量（期望均方反解；保留交互分支以 MS_PO 为比较项，合并分支以合并
误差均方为比较项）：

- 保留交互：σ²_e = MS_E；σ²_PO = (MS_PO − MS_E)/n；
  σ²_O = (MS_O − MS_PO)/(a·n)；σ²_P = (MS_P − MS_PO)/(b·n)
- 合并后：  σ²_e = MS_E′；σ²_PO 不存在；
  σ²_O = (MS_O − MS_E′)/(a·n)；σ²_P = (MS_P − MS_E′)/(b·n)

负的方差分量：原估计保留在 ``raw`` 中供解释；进入 GRR/总变差/比例指标前
**各自独立截到零**（不按比例分摊、不把负值转嫁给其他分量）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from scipy.stats import f as _f_dist

# 交互检验决策
DECISION_KEEP = "keep_interaction"
DECISION_POOL = "pool_into_error"

# 方差分量估计分支
BRANCH_WITH_INTERACTION = "with_interaction"
BRANCH_POOLED = "pooled_error"

# ndc 公式系数（需求指定 1.41 ≈ √2，不用更精确的值替换）
NDC_FACTOR = 1.41

# 总平方和分解核对的相对容差
_SS_CHECK_RTOL = 1e-9


@dataclass(frozen=True)
class AnovaTerm:
    """ANOVA 表中的一行（total 行没有均方）。"""

    ss: float
    df: int
    ms: Optional[float]


@dataclass(frozen=True)
class InteractionTest:
    """交互项 F 检验结果与分支决策。"""

    alpha: float
    f_statistic: Optional[float]  # None：0/0 不可判定或发散，见 reason
    df_num: int
    df_den: int
    p_value: Optional[float]
    decision: str  # DECISION_KEEP / DECISION_POOL
    reason: str


@dataclass(frozen=True)
class VarianceComponents:
    """方差分量：raw 为原估计（可为负），used 为截断后（≥0）。"""

    branch: str
    raw: Dict[str, Optional[float]]  # part/operator/interaction/repeatability
    used: Dict[str, float]
    truncated: Tuple[str, ...]  # 被截到零的分量名


@dataclass(frozen=True)
class AnovaResult:
    """一次平衡交叉 GRR ANOVA 的完整统计结果。"""

    part: AnovaTerm
    operator: AnovaTerm
    interaction: AnovaTerm
    error: AnovaTerm
    total: AnovaTerm  # ms 恒为 None
    ss_check_diff: float  # |SS_T − 四项和|
    test: InteractionTest
    pooled_error: Optional[AnovaTerm]  # 仅合并分支非 None
    components: VarianceComponents


def _mean(values: Sequence[float]) -> float:
    """math.fsum 求和：结果与元素顺序无关，保证行排列不变性。"""
    return math.fsum(values) / len(values)


def run_anova(cell_values: Dict[Tuple[str, str], Sequence[float]],
              parts: Sequence[str], operators: Sequence[str],
              repeats: int, alpha: float) -> AnovaResult:
    """对完整平衡交叉设计执行 ANOVA 与方差分量估计。

    ``cell_values`` 以 (part_id, operator_id) 为键、该格 ``repeats`` 个读数
    （已换算到试验量纲）为值；调用方保证设计完整。
    """
    a, b, n = len(parts), len(operators), repeats
    if a < 3 or b < 2 or n < 2:
        raise ValueError("设计不满足最小规模（≥3 零件、≥2 操作员、≥2 重复）")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha 必须在 (0,1) 内: {alpha}")

    cell_mean: Dict[Tuple[str, str], float] = {}
    for part in parts:
        for op in operators:
            values = cell_values[(part, op)]
            if len(values) != n:
                raise ValueError(f"格 ({part}, {op}) 有 {len(values)} 行，应为 {n}")
            cell_mean[(part, op)] = _mean(values)

    part_mean = {p: _mean([cell_mean[(p, o)] for o in operators]) for p in parts}
    op_mean = {o: _mean([cell_mean[(p, o)] for p in parts]) for o in operators}
    grand = _mean([cell_mean[(p, o)] for p in parts for o in operators])

    ss_part = b * n * math.fsum((part_mean[p] - grand) ** 2 for p in parts)
    ss_op = a * n * math.fsum((op_mean[o] - grand) ** 2 for o in operators)
    ss_int = n * math.fsum(
        (cell_mean[(p, o)] - part_mean[p] - op_mean[o] + grand) ** 2
        for p in parts for o in operators
    )
    ss_err = math.fsum(
        (v - cell_mean[(p, o)]) ** 2
        for p in parts for o in operators for v in cell_values[(p, o)]
    )
    ss_total = math.fsum(
        (v - grand) ** 2
        for p in parts for o in operators for v in cell_values[(p, o)]
    )

    df_part, df_op = a - 1, b - 1
    df_int = (a - 1) * (b - 1)
    df_err = a * b * (n - 1)
    df_total = a * b * n - 1

    ms_part = ss_part / df_part
    ms_op = ss_op / df_op
    ms_int = ss_int / df_int
    ms_err = ss_err / df_err

    ss_check_diff = abs(ss_total - (ss_part + ss_op + ss_int + ss_err))

    # --- 交互项 F 检验（含零分母规则） ---
    if ms_err == 0.0:
        if ms_int == 0.0:
            test = InteractionTest(
                alpha=alpha, f_statistic=None, df_num=df_int, df_den=df_err,
                p_value=None, decision=DECISION_KEEP,
                reason="误差均方与交互均方均为 0（0/0），F 不可判定；"
                       "保守保留交互项",
            )
        else:
            test = InteractionTest(
                alpha=alpha, f_statistic=None, df_num=df_int, df_den=df_err,
                p_value=0.0, decision=DECISION_KEEP,
                reason="误差均方为 0 而交互均方为正值，F 发散至无穷大；"
                       "按显著交互处理，保留交互项",
            )
    else:
        f_stat = ms_int / ms_err
        p_value = float(_f_dist.sf(f_stat, df_int, df_err))
        if p_value < alpha:
            test = InteractionTest(
                alpha=alpha, f_statistic=f_stat, df_num=df_int, df_den=df_err,
                p_value=p_value, decision=DECISION_KEEP,
                reason=f"p={p_value:.6g} < alpha={alpha}，交互显著，保留交互项",
            )
        else:
            test = InteractionTest(
                alpha=alpha, f_statistic=f_stat, df_num=df_int, df_den=df_err,
                p_value=p_value, decision=DECISION_POOL,
                reason=f"p={p_value:.6g} >= alpha={alpha}，交互不显著，"
                       f"交互平方和与自由度合并入误差后重新估计",
            )

    # --- 方差分量 ---
    pooled: Optional[AnovaTerm] = None
    if test.decision == DECISION_POOL:
        branch = BRANCH_POOLED
        pooled_ss = ss_int + ss_err
        pooled_df = df_int + df_err
        pooled_ms = pooled_ss / pooled_df
        pooled = AnovaTerm(ss=pooled_ss, df=pooled_df, ms=pooled_ms)
        raw = {
            "repeatability": pooled_ms,
            "operator": (ms_op - pooled_ms) / (a * n),
            "interaction": None,  # 交互已合并入误差，不再单独存在
            "part": (ms_part - pooled_ms) / (b * n),
        }
    else:
        branch = BRANCH_WITH_INTERACTION
        raw = {
            "repeatability": ms_err,
            "operator": (ms_op - ms_int) / (a * n),
            "interaction": (ms_int - ms_err) / n,
            "part": (ms_part - ms_int) / (b * n),
        }

    used: Dict[str, float] = {}
    truncated = []
    for name, value in raw.items():
        if value is None:
            continue
        if value < 0.0:
            truncated.append(name)
            used[name] = 0.0
        else:
            used[name] = value
    if branch == BRANCH_POOLED:
        used["interaction"] = 0.0

    components = VarianceComponents(branch=branch, raw=raw, used=used,
                                    truncated=tuple(truncated))
    return AnovaResult(
        part=AnovaTerm(ss_part, df_part, ms_part),
        operator=AnovaTerm(ss_op, df_op, ms_op),
        interaction=AnovaTerm(ss_int, df_int, ms_int),
        error=AnovaTerm(ss_err, df_err, ms_err),
        total=AnovaTerm(ss_total, df_total, None),
        ss_check_diff=ss_check_diff,
        test=test,
        pooled_error=pooled,
        components=components,
    )


def ss_decomposition_ok(result: AnovaResult) -> bool:
    """总平方和分解核对：|SS_T − 四项和| 在相对容差内。"""
    scale = max(1.0, abs(result.total.ss))
    return result.ss_check_diff <= _SS_CHECK_RTOL * scale


# --------------------------------------------------------------------------- #
# GRR 指标
# --------------------------------------------------------------------------- #
def undefined(reason: str) -> Dict[str, str]:
    """不可定义比值的统一表示：明确原因，不写成 0、无穷或可用结论。"""
    return {"undefined": reason}


def build_metrics(components: VarianceComponents,
                  spec_lower: Optional[float],
                  spec_upper: Optional[float]) -> Dict[str, object]:
    """由截断后的方差分量计算 GRR 指标。

    - 贡献率（contribution_pct）：**方差**占总方差的比例；
    - %study var：6σ 口径下**标准差**占总标准差的比例（6 已约去）；
    两者口径不同，不混用。
    """
    ev = components.used["repeatability"]
    av = components.used["operator"] + components.used["interaction"]
    grr = ev + av
    pv = components.used["part"]
    tv = grr + pv

    variance = {"repeatability": ev, "reproducibility": av, "grr": grr,
                "part": pv, "total": tv}
    stddev = {name: math.sqrt(v) for name, v in variance.items()}
    study_var_6sigma = {name: 6.0 * s for name, s in stddev.items()}

    if tv > 0.0:
        contribution = {name: 100.0 * v / tv for name, v in
                        (("repeatability", ev), ("reproducibility", av),
                         ("grr", grr), ("part", pv))}
    else:
        contribution = {name: undefined("总方差 σ²_TV = 0，方差贡献率不可定义"
                                        "（分母为零）")
                        for name in ("repeatability", "reproducibility",
                                     "grr", "part")}

    if stddev["total"] > 0.0:
        pct_study = {name: 100.0 * stddev[name] / stddev["total"]
                     for name in ("repeatability", "reproducibility",
                                  "grr", "part")}
    else:
        pct_study = {name: undefined("总标准差 σ_TV = 0，6σ 口径标准差比例"
                                     "不可定义（分母为零）")
                     for name in ("repeatability", "reproducibility",
                                  "grr", "part")}

    if spec_lower is None or spec_upper is None:
        pct_tol = None  # 无规格：不算规格占比
    else:
        width = spec_upper - spec_lower
        if width <= 0.0:
            raise ValueError(f"规格宽度必须为正: upper({spec_upper}) <= lower({spec_lower})")
        pct_tol = 100.0 * study_var_6sigma["grr"] / width

    if stddev["grr"] > 0.0:
        ndc: object = math.floor(NDC_FACTOR * stddev["part"] / stddev["grr"])
    else:
        ndc = undefined("GRR 标准差 σ_GRR = 0，ndc = floor(1.41·σ_P/σ_GRR) "
                        "不可定义（分母为零）")

    return {
        "variance": variance,
        "contribution_pct": contribution,
        "stddev": stddev,
        "study_var_6sigma": study_var_6sigma,
        "pct_study_var": pct_study,
        "pct_tolerance_grr": pct_tol,
        "ndc": ndc,
    }


def collect_undefined(metrics: Dict[str, object]) -> Dict[str, str]:
    """汇总指标中所有不可定义项及其原因（用于结果顶层的快速浏览）。"""
    found: Dict[str, str] = {}

    def walk(prefix: str, node: object) -> None:
        if isinstance(node, dict):
            if set(node) == {"undefined"}:
                found[prefix] = str(node["undefined"])
            else:
                for key, val in node.items():
                    walk(f"{prefix}.{key}" if prefix else str(key), val)

    walk("", metrics)
    return found
