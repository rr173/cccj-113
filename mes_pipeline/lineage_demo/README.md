# 实耗批次谱系（lineage）演示材料

五组自制小数据，覆盖：两种原料混合后部分领用、带损耗的一进多出、分流再
汇合（菱形路径）、完全耗尽的中间批、同批含多个可疑来源、倒序提供操作、
g/kg 混写与恰好耗尽；另有超领一克、质量不守恒、缺引用及成环四类拒绝，
以及修正后的重新装载。

所有命令在仓库根目录执行。本子系统为**纯标准库**实现（`fractions`/
`decimal`/`json`），不依赖 pm4py/pandas/scipy，也**不从工序轨迹、配料建议
或量具试验推测实际用料**——谱系只建立在外购批声明与实际领料/产出记录上。

## 0. 输入文件格式

| 文件 | 说明 |
| --- | --- |
| `materials.json` | `{"batches": [{"batch_id", "material", "initial_mass": {"mass", "unit"}}]}`，声明外购原料批及初始质量 |
| `operations.json` | `{"operations": [{"operation_id", "inputs": [{"batch_id","mass","unit"}], "outputs": [...], "loss_mass": {"mass","unit"}}]}`，每项操作有唯一 operation_id、若干输入实耗、若干新产出与本次损耗 |

- 单位仅 `g` / `kg`（1 kg = 1000 g），可在同一文件甚至同一操作内混写；
- 数量必须是有限十进制：接受十进制字符串（`"0.1"`）与 JSON 整数；
  非整数 JSON 浮点被拒绝，避免二进制浮点参与守恒/超领判定；
- 全部数量必须为正，损耗可为零；输入总量必须**恰好**等于产出加损耗（无容差）；
- 一个产出批只能有一个生产者，不能覆盖外购批或另一操作的产出；
- 记录顺序不代表加工顺序：先按生产者/消费者建依赖再计算，合法倒序不会被拒。

## 1. 三个 CLI 入口

```bash
S=/tmp/line_store
# ① 装载并计算（退出码 0 成功 / 4 输入不合法；失败结果也落盘）
python3 -I -m mes_pipeline.cli --store $S lineage-load \
    --materials lineage_demo/s3_diamond/materials.json \
    --operations lineage_demo/s3_diamond/operations_reverse.json \
    --calc-id demo-s3

# ② 任一已知批的构成查询（生成质量、剩余质量、按外购批拆分的构成与路径）
python3 -I -m mes_pipeline.cli --store $S lineage-composition \
    --calc-id demo-s3 --batch F-1

# ③ 对可疑外购批发起在库召回测算（--suspect 可重复传多个）
python3 -I -m mes_pipeline.cli --store $S lineage-recall \
    --calc-id demo-s3 --suspect RM-X --suspect RM-Y
```

结果保存在 `$S/lineage_calcs/<calc_id>.json`，可重复查询；修正文件后用新
calc_id 重新装载，此前算好的结果（含失败记录）仍保留可读。

## 2. 场景与手工核算值

| 场景 | 目录 | 覆盖要点 |
| --- | --- | --- |
| 混合后部分领用 | `s1_mix_partial` | 10kg RM-A + 5000g RM-B（g/kg 混写），领 6kg+2kg 产 8kg M-1；未领用部分保持原组成 |
| 一进多出带损耗 | `s2_split_loss` | 10kg RM-C → P-1 3kg + P-2 2500g + 损耗 4.5kg；损耗按同一混合组成分配 |
| 分流再汇合 | `s3_diamond` | 操作文件**倒序**提供；M-1 分流为 D-1/D-2（均完全耗尽）后汇合 F-1；同批含双嫌疑 |
| 恰好耗尽 | `s4_exact_exhaust` | 4kg + 1000g 混合后一进多出，两外购批均恰好耗尽，零损耗 |
| 拒绝与修正 | `s5_reject_then_fix` | 超领 1g / 不守恒 / 缺引用 / 成环分别被拒；修正后倒序记录装载成功 |

### s3 菱形手工核算（克，全部精确值）

混合：RM-X 10000g + RM-Y 6000g → M-1 16000g（X 占 5/8，Y 占 3/8）。

| 操作 | 投入 | 产出/损耗 |
| --- | --- | --- |
| OP-BRANCH1 | M-1 8000（X 5000, Y 3000） | D-1 4000（X 2500, Y 1500）+ 损耗 4000（X 2500, Y 1500） |
| OP-BRANCH2 | M-1 6000（X 3750, Y 2250） | D-2 3000（X 1875, Y 1125）+ 损耗 3000（X 1875, Y 1125） |
| OP-MERGE | D-1 4000 + D-2 3000 | F-1 7000（X 4375, Y 2625） |

- F-1 的 RM-X 由两支**相加**：2500 + 1875 = 4375g（路径分别保留，不去重）；
- M-1 余额 2000g（X 1250 + Y 750）；D-1/D-2 已完全耗尽，不进隔离总量；
- 双嫌疑召回隔离总量 = F-1 7000 + M-1 余 2000 = **9000g**（F-1 虽含两种
  可疑原料只隔离一次）；
- 全局账：RM-X 10000 = 剩余 5625（M-1 1250 + F-1 4375）+ 损耗 4375；
  RM-Y 6000 = 剩余 3375 + 损耗 2625。

### s5 四类拒绝（均退出码 4，错误定位到原记录）

```bash
D=lineage_demo/s5_reject_then_fix
for v in overdraw_1g unbalanced missing_ref cycle; do
  python3 -I -m mes_pipeline.cli --store $S lineage-load \
      --materials $D/materials.json --operations $D/operations_$v.json \
      --calc-id bad-$v
done
# 修正后（倒序：OP-STEP2 写在 OP-STEP1 之前）装载成功
python3 -I -m mes_pipeline.cli --store $S lineage-load \
    --materials $D/materials.json \
    --operations $D/operations_fixed_reverse.json --calc-id demo-s5-fixed
```

## 3. 独立复核

`verify.py` **不调用生产分析函数**：自行解析输入 JSON，用 `Fraction`
独立实现拓扑排序、均匀混合传播、损耗分配与全局账，再与生产结果 JSON
逐项核对原料贡献、余额、损耗和召回总量：

```bash
python3 -I lineage_demo/verify.py verify-all --store /tmp/line_verify
```

## 4. 适用假设（结果 JSON 的 assumptions 段同步声明）

1. **理想均匀混合**：批次内部组成处处相同，部分领用按各外购原料占比分摊；
2. 同一操作的各产出与损耗按全部投入的混合组成等比例分配；质量损耗只减量，
   不选择性去除某种原料；
3. 菱形路径上同一原料的各支贡献相加，不去重丢支、不重复整支祖先量；
4. 未领用部分保持原批组成；
5. 全部质量用有理数精确算术（`fractions.Fraction`），守恒/超领/余额无浮点
   容差；展示层除不尽的占比给 28 位小数（HALF_UP）并附精确分数字段与
   `all_decimals_exact` 标志。
