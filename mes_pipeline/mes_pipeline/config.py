"""管线配置：产线时钟与工艺约束规范。

配置必须显式声明，不做隐式时区推断。所有数据类均为不可变值对象。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timezone, timedelta
from pathlib import Path
from typing import FrozenSet, Mapping, Tuple


@dataclass(frozen=True)
class LineClock:
    """单条产线的时钟约定：本地墙钟格式 + 固定 UTC offset（小时）。"""

    line_code: str
    strftime_pattern: str
    utc_offset_hours: float

    @property
    def tzinfo(self) -> timezone:
        return timezone(timedelta(hours=self.utc_offset_hours))

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "LineClock":
        return cls(
            line_code=str(data["line_code"]),
            strftime_pattern=str(data["strftime_pattern"]),
            utc_offset_hours=float(data["utc_offset_hours"]),
        )


@dataclass(frozen=True)
class ProcessSpec:
    """简化有向图工艺规范。

    - edges: 普通前序边 ``(predecessor, successor)``，前序 CLOSE 之后后继才可 OPEN；
    - rework_edges: 返工边，允许后继在前序已完成后再次出现；
    - optional: 允许缺失的操作（不参与“缺少必需工序”判定）；
    - required: 必须至少出现一次的操作（可选集合的补集默认全部必需，可显式列出）。

    并行分支通过共享前驱/后继的多条边自然表达；可选步骤通过 ``optional`` 表达。
    """

    edges: FrozenSet[Tuple[str, str]] = field(default_factory=frozenset)
    rework_edges: FrozenSet[Tuple[str, str]] = field(default_factory=frozenset)
    optional: FrozenSet[str] = field(default_factory=frozenset)
    required: FrozenSet[str] = field(default_factory=frozenset)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ProcessSpec":
        edges = frozenset((str(a), str(b)) for a, b in data.get("edges", []))
        rework = frozenset((a, b) for a, b in data.get("rework_edges", []))
        optional = frozenset(str(x) for x in data.get("optional", []))
        required = frozenset(str(x) for x in data.get("required", []))
        return cls(edges=edges, rework_edges=rework, optional=optional, required=required)

    def all_operations(self) -> FrozenSet[str]:
        nodes = set(self.required) | set(self.optional)
        for a, b in self.edges | self.rework_edges:
            nodes.add(a)
            nodes.add(b)
        return frozenset(nodes)

    def predecessors(self, op: str) -> FrozenSet[str]:
        return frozenset(a for a, b in self.edges if b == op)

    def rework_predecessors(self, op: str) -> FrozenSet[str]:
        return frozenset(a for a, b in self.rework_edges if b == op)

    def to_dict(self) -> dict:
        return {
            "edges": sorted(map(list, self.edges)),
            "rework_edges": sorted(map(list, self.rework_edges)),
            "optional": sorted(self.optional),
            "required": sorted(self.required),
        }


@dataclass(frozen=True)
class PipelineConfig:
    clocks: Mapping[str, LineClock]
    process_spec: ProcessSpec

    @classmethod
    def load(cls, path: str | Path) -> "PipelineConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        clocks = {
            c["line_code"]: LineClock.from_dict(c) for c in raw["line_clocks"]
        }
        return cls(clocks=clocks, process_spec=ProcessSpec.from_dict(raw["process_spec"]))
