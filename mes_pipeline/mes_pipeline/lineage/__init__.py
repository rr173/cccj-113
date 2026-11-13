"""实耗批次谱系（actual-consumption batch genealogy）子包。

只依据两份**实际记录**重建数量流向：

- 外购原料批声明文件（原料批 ID、物料、初始质量）；
- 加工操作记录文件（operation_id、实耗输入批及质量、新产出批及质量、损耗质量）。

不读取、不推测任何 MES 工序轨迹、配料建议或量具试验结果。全部质量运算采用
有理数精确算术（:class:`fractions.Fraction`），质量守恒与累计超领判定不设
浮点容差；适用假设为理想均匀混合，详见 :mod:`mes_pipeline.lineage.engine`。
"""
