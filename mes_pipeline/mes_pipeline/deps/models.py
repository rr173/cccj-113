"""依赖发现与规则核查的不可变值对象。

本模块只定义数据结构与口径常量，不做任何 I/O 或搜索。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# 空值口径
NULL_EQUAL = "equal"
NULL_IGNORE = "ignore"
NULL_POLICIES = (NULL_EQUAL, NULL_IGNORE)

# 单条规则的核查结论
STATUS_HOLDS = "HOLDS"
STATUS_VIOLATED = "VIOLATED"
STATUS_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

# 默认规模上限（超出即拒绝，绝不把部分搜索标成完整）
DEFAULT_MAX_COLUMNS = 32
DEFAULT_MAX_ROWS = 200_000
DEFAULT_MAX_MAX_SCAN_CELLS = 20_000_000


@dataclass(frozen=True)
class CellValue:
    """一次读取到的值：``text`` 为原始字符串（含空字符串），null 用 ``is_null``。"""

    text: Optional[str]
    is_null: bool

    @staticmethod
    def from_text(text: str) -> "CellValue":
        return CellValue(text=text, is_null=False)

    @staticmethod
    def null() -> "CellValue":
        return CellValue(text=None, is_null=True)


@dataclass(frozen=True)
class TableRow:
    """一条资料记录：唯一 row_id + 按列顺序的原始值。"""

    row_id: str
    values: Tuple[CellValue, ...]


@dataclass(frozen=True)
class Dataset:
    """解析并校验通过的单表资料。

    ``columns`` 是资料声明的列顺序（列清单文件或行文件表头的出现顺序）；
    ``values`` 用 ``None`` 表示 null。列名大小写与空格不做改写，按字面匹配。
    """

    table_id: str
    columns: Tuple[str, ...]
    row_ids: Tuple[str, ...]
    values: Tuple[Tuple[Optional[str], ...], ...]
    source: str = ""

    @property
    def row_count(self) -> int:
        return len(self.row_ids)

    @property
    def column_count(self) -> int:
        return len(self.columns)


@dataclass(frozen=True)
class ColumnIndex:
    """列等值索引：按值排序后的行序号与游程边界（一份列压缩字典）。

    排序键是 ``(值, row_id)``，字符串按 Python 的 Unicode 码位序（与
    ``sorted()`` 一致，不是 UTF-16 码元序）；null 单独成段，排在字符串之前。
    记录以 0 为基，此处只给索引，不复制原始值。
    """

    column: int
    rows: Tuple[int, ...]          # 按 (值,row_id) 排序后的记录序号
    run_starts: Tuple[int, ...]    # 每个值游程在 rows 中的起始位置
    run_values: Tuple[Optional[str], ...]  # 每个游程的值（None 表示 null）
    null_count: int


@dataclass(frozen=True)
class Evidence:
    """一条规则的参与/排除明细与核查结论（可嵌套到最小性摘要）。

    ``excluded_null_left`` 是左部（键则指键列）含 null 而被排除的行数；
    ``excluded_null_right`` 是左部不含 null、但右部含 null 而被排除的行数；
    ``excluded_other`` 预留给非空值原因的排除（目前恒为 0）。三者之和等于
    ``row_total - participating``。
    """

    participating: int
    row_total: int
    excluded_null_left: int
    excluded_null_right: int
    excluded_other: int
    min_rows: int
    status: str
    null_policy: str

    @property
    def excluded_null(self) -> int:
        return self.excluded_null_left + self.excluded_null_right

    @property
    def excluded_total(self) -> int:
        return self.excluded_null + self.excluded_other

    def as_dict(self) -> dict:
        return {
            "null_policy": self.null_policy,
            "participating_rows": self.participating,
            "excluded_rows": self.excluded_total,
            "excluded_null_rows": self.excluded_null,
            "excluded_null_left_rows": self.excluded_null_left,
            "excluded_null_right_rows": self.excluded_null_right,
            "excluded_other_rows": self.excluded_other,
            "row_total": self.row_total,
            "min_rows": self.min_rows,
            "status": self.status,
        }


@dataclass(frozen=True)
class Violation:
    """一对实际反例行（row_id 按 Unicode 顺序取字典序最小的一对）。

    ``left`` 存放参与比较的列值（键为键值/依赖左部），``right`` 只在依赖
    反例中给出右部值（两侧不同）。``values`` 保留涉及字段的原始字面值。
    """

    row_id_a: str
    row_id_b: str
    left_a: Tuple[Optional[str], ...]
    left_b: Tuple[Optional[str], ...]
    right_a: Optional[str] = None
    right_b: Optional[str] = None

    def as_dict(self, left_columns, right_column: Optional[str] = None) -> dict:
        out = {
            "row_id_a": self.row_id_a,
            "row_id_b": self.row_id_b,
            "left_columns": list(left_columns),
            "left_values_a": list(self.left_a),
            "left_values_b": list(self.left_b),
        }
        if right_column is not None:
            out["right_column"] = right_column
            out["right_value_a"] = self.right_a
            out["right_value_b"] = self.right_b
        return out


@dataclass(frozen=True)
class DeleteSummary:
    """删除最小键/依赖左部中某一列后的核查摘要（解释「更小组合为什么不够」）。"""

    deleted_column: str
    remaining_columns: Tuple[str, ...]
    evidence: Evidence
    violation: Optional[Violation] = None
    note: str = ""

    def as_dict(self) -> dict:
        out = {
            "deleted_column": self.deleted_column,
            "remaining_left": list(self.remaining_columns),
            "check": self.evidence.as_dict(),
            "why_not_smaller": self.note or _delete_reason(self.evidence),
        }
        if self.violation is not None:
            out["counterexample"] = self.violation.as_dict(self.remaining_columns)
        return out


@dataclass(frozen=True)
class UniqueKey:
    """发现的最小唯一键。"""

    columns: Tuple[str, ...]
    evidence: Evidence
    delete_summaries: Tuple[DeleteSummary, ...] = ()

    def as_dict(self) -> dict:
        return {
            "columns": list(self.columns),
            "length": len(self.columns),
            "check": self.evidence.as_dict(),
            "minimality": [d.as_dict() for d in self.delete_summaries],
        }


@dataclass(frozen=True)
class FunctionalDependency:
    """发现的最小函数依赖 X → A（``left`` 允许为空，表示常量列）。"""

    left: Tuple[str, ...]
    right: str
    evidence: Evidence
    delete_summaries: Tuple[DeleteSummary, ...] = ()

    def as_dict(self) -> dict:
        return {
            "left": list(self.left),
            "right": self.right,
            "left_length": len(self.left),
            "expression": ("{} → {}".format("∅" if not self.left else ", ".join(self.left),
                                             self.right)),
            "check": self.evidence.as_dict(),
            "minimality": [d.as_dict() for d in self.delete_summaries],
        }


def _delete_reason(evidence: Evidence) -> str:
    if evidence.status == STATUS_NOT_APPLICABLE:
        return "空组合不是候选键，这里没有更小组合可核查"
    if evidence.status == STATUS_HOLDS:
        return "删除该列后仍然成立：原结果本应更短或不是最小"
    if evidence.status == STATUS_VIOLATED:
        return "删除该列后出现实际反例，因此该列不可省"
    return ("删除该列后参与行数不足 min_rows，无法支持更小组合；"
            "这不是「没有冲突」，而是「资料不足」")
