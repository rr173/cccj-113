"""DESADV 测试共用的最小 EDI 构造器（与生产代码独立，避免循环验证）。"""
from __future__ import annotations

from typing import List, Sequence, Union

Element = Union[str, Sequence[str]]


class EdiTextBuilder:
    """按服务字符拼段，结构组件用 list/tuple 传入，数据文本自动转义。"""

    def __init__(self, chars: dict | None = None, una: bool = True) -> None:
        self.c = chars or {"comp": ":", "elem": "+", "decimal": ".",
                           "release": "?", "term": "'"}
        self.parts: List[bytes] = []
        if una:
            self.parts.append(
                (f"UNA{self.c['comp']}{self.c['elem']}{self.c['decimal']}"
                 f"{self.c['release']} {self.c['term']}").encode("utf-8"))

    def _escape(self, text: str) -> str:
        service = {self.c["comp"], self.c["elem"], self.c["release"],
                   self.c["term"]}
        return "".join(
            (self.c["release"] + ch) if ch in service else ch for ch in text)

    def seg(self, tag: str, *elements: Element) -> "EdiTextBuilder":
        rendered = [tag]
        for element in elements:
            if isinstance(element, (list, tuple)):
                rendered.append(self.c["comp"].join(
                    self._escape(str(c)) for c in element))
            else:
                rendered.append(self._escape(str(element)))
        self.parts.append(
            (self.c["elem"].join(rendered) + self.c["term"]).encode("utf-8"))
        return self

    def raw(self, text: str) -> "EdiTextBuilder":
        self.parts.append(text.encode("utf-8"))
        return self

    def bytes(self) -> bytes:
        return b"\n".join(self.parts) + b"\n"


def minimal_exchange(chars: dict | None = None,
                     control: str = "EX1") -> bytes:
    """一个结构最简但合法的单消息交换（根包装 + 一行 + 一备注）。"""
    c = chars or {"comp": ":", "elem": "+", "decimal": ".",
                  "release": "?", "term": "'"}
    b = EdiTextBuilder(c)
    b.seg("UNB", ["UNOC", "3"], "S1", "R1", ["260101", "0900"], control)
    b.seg("UNH", "M1", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "DN1", "9")
    b.seg("DTM", ["137", "20260101", "102"])
    b.seg("NAD", "SU", ["SUP1", "", "92"])
    b.seg("NAD", "BY", ["BUY1", "", "92"])
    b.seg("CPS", "P1", "")
    b.seg("PAC", "1", "", "CT")
    b.seg("LIN", "1", "", ["PART1", "IN"])
    b.seg("QTY", ["12", "2", "PCE"])
    b.seg("FTX", "AAI", "", "", "备注 中文")
    b.seg("UNT", "11", "M1")
    b.seg("UNZ", "1", control)
    return b.bytes()
