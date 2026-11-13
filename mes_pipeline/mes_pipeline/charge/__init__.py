"""配料计算：按库存、化验结果与炉次要求求解最低成本投料方案。

子包与 MES 活动轨迹管线相互独立：求解输入只有原料库存表、化验表、
证书登记表与炉次要求四类质量/数量数据，不读取任何事件轨迹。

模块划分：
- ``models``  : 不可变值对象（库存批、化验区间、炉次要求、求解结果）；
- ``io``      : 读取并校验 CSV/JSON 输入，数据问题一律拒绝计算；
- ``solver``  : 纯 Python 两阶段单纯形 + 分支定界 MILP，以及冲突约束集（IIS）提取；
- ``service`` : 编排求解、保守口径核算、结果落盘与读取。
"""
from __future__ import annotations

from .io import ChargeInputError, load_charge_input
from .service import solve_charge, load_result

__all__ = [
    "ChargeInputError",
    "load_charge_input",
    "solve_charge",
    "load_result",
]
