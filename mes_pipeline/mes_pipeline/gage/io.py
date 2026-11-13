"""量具 R&R 试验输入的读取与校验。

输入两类文件（零件×操作员×重复测量的试验数据，与 MES 工序轨迹、
配料计算结果无关）：

- ``study.json``        试验定义：study_id、特性、量纲、零件清单、操作员清单、
                        每格重复次数、可选规格上下限
- ``observations.csv``  观测行：study_id,part_id,operator_id,repeat_id,value,unit

校验原则：只接受**完整的平衡交叉设计**——至少 3 个零件、2 个操作员，
每个零件×操作员格恰好有 ``repeats`` 次（≥2）重复测量。定位到同一测量格的
重复行、未声明的零件/人员、缺格、非有限读数、不兼容单位一律说明位置并拒绝；
不补齐缺格、不平均重复行、不悄悄丢弃某个零件或操作员。

问题分两层抛出：
- :class:`GageInputError`   输入错误（解析/单位/有限性/声明外 ID/重复行/规格）
- :class:`GageDesignError`  设计不完整（规模不足/缺格/重复次数不齐）
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .models import (
    GageInput,
    Observation,
    StudySpec,
    convert_length,
    normalize_unit,
)

OBS_COLUMNS = ("study_id", "part_id", "operator_id", "repeat_id", "value", "unit")


class GageInputError(ValueError):
    """输入数据不合法：携带全部问题描述，拒绝本次分析。"""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("输入数据不合法，拒绝分析:\n" + "\n".join(f"- {p}" for p in self.problems))


class GageDesignError(ValueError):
    """试验设计不满足完整平衡交叉要求：携带全部问题描述，拒绝本次分析。"""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("试验设计不完整，拒绝分析:\n" + "\n".join(f"- {p}" for p in self.problems))


def digest_files(paths: Sequence[str | Path]) -> str:
    """输入文件字节级 SHA-256（按文件名排序拼接），用于结果可追溯。"""
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# 试验定义
# --------------------------------------------------------------------------- #
def load_study(path: str | Path, problems: List[str]) -> Optional[StudySpec]:
    """读取试验定义 JSON；问题写入 ``problems``，无法使用时返回 None。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"study.json: 无法解析: {exc}")
        return None
    if not isinstance(raw, dict):
        problems.append("study.json: 顶层必须是对象")
        return None

    study_id = str(raw.get("study_id") or "").strip()
    if not study_id:
        problems.append("study.json: study_id 缺失")
        study_id = "UNKNOWN"
    characteristic = str(raw.get("characteristic") or "").strip() or study_id

    unit = normalize_unit(str(raw.get("unit") or ""))
    if unit is None:
        problems.append(f"study.json: 量纲不明: {raw.get('unit')!r}（应为 mm/μm）")
        unit = "mm"

    def _str_list(key: str) -> Tuple[str, ...]:
        items = raw.get(key)
        if not isinstance(items, list) or not items:
            problems.append(f"study.json: {key} 必须是非空数组")
            return ()
        out: List[str] = []
        for item in items:
            text = str(item).strip()
            if not text:
                problems.append(f"study.json: {key} 含空条目")
                continue
            if text in out:
                problems.append(f"study.json: {key} 重复声明: {text}")
                continue
            out.append(text)
        return tuple(out)

    parts = _str_list("parts")
    operators = _str_list("operators")

    repeats_raw = raw.get("repeats")
    repeats = 0
    if isinstance(repeats_raw, bool) or not isinstance(repeats_raw, int):
        problems.append(f"study.json: repeats 必须是整数: {repeats_raw!r}")
    elif repeats_raw < 1:
        problems.append(f"study.json: repeats 必须为正整数: {repeats_raw}")
    else:
        repeats = repeats_raw

    spec_lower = spec_upper = None
    spec = raw.get("spec")
    if spec is not None:
        if not isinstance(spec, dict):
            problems.append("study.json: spec 必须是对象（lower/upper）")
        else:
            lo_raw, hi_raw = spec.get("lower"), spec.get("upper")
            for name, val in (("lower", lo_raw), ("upper", hi_raw)):
                if val is not None and (
                    isinstance(val, bool)
                    or not isinstance(val, (int, float))
                    or not math.isfinite(float(val))
                ):
                    problems.append(f"study.json: spec.{name} 不是有限数值: {val!r}")
            if isinstance(lo_raw, (int, float)) and not isinstance(lo_raw, bool) \
                    and math.isfinite(float(lo_raw)):
                spec_lower = float(lo_raw)
            if isinstance(hi_raw, (int, float)) and not isinstance(hi_raw, bool) \
                    and math.isfinite(float(hi_raw)):
                spec_upper = float(hi_raw)
            if (spec_lower is None) != (spec_upper is None):
                problems.append("study.json: 规格上下限必须同时给出或同时省略")
                spec_lower = spec_upper = None
            elif spec_lower is not None and spec_upper is not None \
                    and spec_upper <= spec_lower:
                problems.append(
                    f"study.json: 规格宽度必须为正: "
                    f"upper({spec_upper}) <= lower({spec_lower})，拒绝"
                )
                spec_lower = spec_upper = None

    if problems:
        return None
    return StudySpec(
        study_id=study_id,
        characteristic=characteristic,
        unit=unit,
        parts=parts,
        operators=operators,
        repeats=repeats,
        spec_lower=spec_lower,
        spec_upper=spec_upper,
    )


# --------------------------------------------------------------------------- #
# 观测行
# --------------------------------------------------------------------------- #
def _read_csv(path: str | Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def load_observations(path: str | Path, study: StudySpec,
                      problems: List[str]) -> Tuple[Observation, ...]:
    """读取观测行并逐行校验；问题写入 ``problems`` 而不是立即抛出。"""
    try:
        rows = _read_csv(path)
    except OSError as exc:
        problems.append(f"observations.csv: 无法读取: {exc}")
        return ()
    if not rows:
        problems.append("observations.csv: 没有任何观测行")
        return ()

    declared_parts = set(study.parts)
    declared_ops = set(study.operators)
    seen_cells: Dict[Tuple[str, str, int], int] = {}  # (part, op, repeat) -> 首个行号
    out: List[Observation] = []

    for i, row in enumerate(rows, start=2):  # 第 1 行是表头
        where = f"observations.csv 第{i}行"
        missing_cols = [c for c in OBS_COLUMNS if c not in row]
        if missing_cols:
            problems.append(f"{where}: 缺少列 {missing_cols}（应为 {list(OBS_COLUMNS)}）")
            continue

        row_study = (row.get("study_id") or "").strip()
        part_id = (row.get("part_id") or "").strip()
        operator_id = (row.get("operator_id") or "").strip()
        repeat_raw = (row.get("repeat_id") or "").strip()
        value_raw = (row.get("value") or "").strip()
        unit_raw = (row.get("unit") or "").strip()

        bad = False
        if row_study != study.study_id:
            problems.append(f"{where}: study_id {row_study!r} 与试验定义 "
                            f"{study.study_id!r} 不一致")
            bad = True
        if part_id not in declared_parts:
            problems.append(f"{where}: 零件 {part_id!r} 未在试验定义中声明，"
                            f"不能悄悄纳入或丢弃")
            bad = True
        if operator_id not in declared_ops:
            problems.append(f"{where}: 操作员 {operator_id!r} 未在试验定义中声明，"
                            f"不能悄悄纳入或丢弃")
            bad = True

        repeat_id: Optional[int] = None
        try:
            repeat_id = int(repeat_raw)
            if str(repeat_id) != repeat_raw:
                raise ValueError
            if repeat_id < 1:
                problems.append(f"{where}: repeat_id 必须为正整数: {repeat_raw!r}")
                bad = True
                repeat_id = None
            elif repeat_id > study.repeats:
                problems.append(f"{where}: repeat_id {repeat_id} 超出声明的重复次数 "
                                f"{study.repeats}")
                bad = True
                repeat_id = None
        except ValueError:
            problems.append(f"{where}: repeat_id 不是整数: {repeat_raw!r}")
            bad = True

        value: Optional[float] = None
        try:
            value = float(value_raw)
        except ValueError:
            problems.append(f"{where}: 读数无法解析为数值: {value_raw!r}")
            bad = True
        else:
            if not math.isfinite(value):
                problems.append(f"{where}: 读数必须有限: {value_raw!r}")
                bad = True
                value = None

        unit = normalize_unit(unit_raw)
        if not unit:
            problems.append(f"{where}: 单位不明或与长度量纲不兼容: {unit_raw!r}"
                            f"（应为 mm/μm）")
            bad = True

        if bad or repeat_id is None or value is None or unit is None:
            continue

        key = (part_id, operator_id, repeat_id)
        if key in seen_cells:
            problems.append(
                f"{where}: 定位到同一测量格的重复行（零件 {part_id} × 操作员 "
                f"{operator_id} × 第 {repeat_id} 次，首次出现于第 "
                f"{seen_cells[key]} 行），不平均、不取舍，拒绝"
            )
            continue
        seen_cells[key] = i

        out.append(Observation(
            row=i,
            study_id=row_study,
            part_id=part_id,
            operator_id=operator_id,
            repeat_id=repeat_id,
            value=value,
            unit=unit,
            canonical_value=convert_length(value, unit, study.unit),
        ))
    return tuple(out)


# --------------------------------------------------------------------------- #
# 设计完整性
# --------------------------------------------------------------------------- #
def validate_design(study: StudySpec, observations: Tuple[Observation, ...],
                    problems: List[str]) -> None:
    """校验完整平衡交叉设计；问题写入 ``problems``。"""
    if len(study.parts) < 3:
        problems.append(f"试验设计：零件数 {len(study.parts)} 少于 3 个，"
                        f"无法把零件真实差异从量具波动中分离")
    if len(study.operators) < 2:
        problems.append(f"试验设计：操作员数 {len(study.operators)} 少于 2 个，"
                        f"无法估计再现性")
    if study.repeats < 2:
        problems.append(f"试验设计：每格重复次数 {study.repeats} 少于 2 次，"
                        f"无法估计重复性")

    have: Dict[Tuple[str, str], set[int]] = {}
    for obs in observations:
        have.setdefault((obs.part_id, obs.operator_id), set()).add(obs.repeat_id)

    expected = set(range(1, study.repeats + 1))
    for part in study.parts:
        for op in study.operators:
            got = have.get((part, op), set())
            missing = sorted(expected - got)
            if missing:
                problems.append(
                    f"试验设计：缺格——零件 {part} × 操作员 {op} 缺第 "
                    f"{missing} 次重复（应有 {study.repeats} 次，实有 "
                    f"{len(got & expected)} 次），不补齐、不少算该格"
                )


# --------------------------------------------------------------------------- #
# 汇总入口
# --------------------------------------------------------------------------- #
def load_gage_input(study_path: str | Path,
                    observations_path: str | Path) -> GageInput:
    """读取并交叉校验全部输入；任何数据问题都使本次分析被拒绝。

    先抛 :class:`GageInputError`（输入错误），输入层面干净后再抛
    :class:`GageDesignError`（设计不完整）。
    """
    problems: List[str] = []
    study = load_study(study_path, problems)
    if study is None:
        raise GageInputError(problems)
    observations = load_observations(observations_path, study, problems)
    if problems:
        raise GageInputError(problems)

    design_problems: List[str] = []
    validate_design(study, observations, design_problems)
    if design_problems:
        raise GageDesignError(design_problems)
    return GageInput(study=study, observations=observations)
