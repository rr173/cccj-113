# 量具 R&R（交叉型）演示材料

六组自制小数据，覆盖：交互显著保留、交互合并入误差、负方差分量原估计、
全相等读数导致零分母、重复行/缺格被拒绝后修正重算、各类非法输入拒绝。

所有命令在仓库根目录执行，解释器为 `.venv/bin/python`（项目已 editable 安装，
`-I` 隔离模式可正常运行）。本分析的输入只有试验定义与观测行，
**不从 MES 工序轨迹或配料计算结果中推导样本**。

## 0. 输入文件格式

| 文件 | 说明 |
| --- | --- |
| `study.json` | 试验定义：`study_id`、`characteristic`、`unit`（`mm`/`μm`）、`parts`（≥3）、`operators`（≥2）、`repeats`（每格重复次数，≥2，可大于 2）、可选 `spec: {"lower":…,"upper":…}`（规格上下限，须同时给出且宽度为正） |
| `observations.csv` | `study_id,part_id,operator_id,repeat_id,value,unit`；每个 零件×操作员×重复次数 格恰好一行；行内单位可为 `mm` 或 `μm`（含 `um`/`µm` 别名），统一换算到试验量纲 |

只分析**完整的平衡交叉设计**：每个零件都由每名操作员重复测量相同次数。
定位到同一测量格的重复行、未声明的零件/人员、缺格、非有限读数、不兼容
单位都会说明位置并拒绝——不补齐、不平均重复行、不悄悄少算一个人。

## 1. 运行与查看

```bash
S=/tmp/grr_store
# 计算并保存（--alpha 缺省 0.25；--calc-id 可省略，缺省为 <study_id>-<时间戳>）
.venv/bin/python -I -m mes_pipeline.cli --store $S grr-run \
    --study grr_demo/s1_interaction_kept/study.json \
    --observations grr_demo/s1_interaction_kept/observations.csv \
    --calc-id demo-s1
# 按试验 ID 查看该次结果（成功/部分成功/失败均可查看）
.venv/bin/python -I -m mes_pipeline.cli --store $S grr-show --calc-id demo-s1
```

退出码：`0` 计算完成（含个别统计量不可定义的 `OK_PARTIAL`）/
`4` 输入不合法（`INVALID_INPUT`）/ `6` 设计不完整（`INCOMPLETE_DESIGN`）。

结果中 `means` 段给出总均值、零件均值、操作员均值与每个交叉格均值，
`observations` 段给出每行的 CSV 行号与换算后读数——可沿任一均方回到
对应的分组均值和测量行。

## 2. 六个场景与预期结论

| 场景 | 目录 | 预期 |
| --- | --- | --- |
| 交互显著，保留交互 | `s1_interaction_kept` | F=16.667，p≈0.0035 < 0.25 → `keep_interaction`；ndc=3 |
| 交互为零，合并入误差 | `s2_interaction_pooled` | F=0，p=1.0 → `pool_into_error`；合并误差 df=8；ndc=5 |
| 负方差分量 | `s3_negative_component` | σ²_操作员 原估计 −2.83e-4 保留于 `raw`，指标中截到 0；无规格不算规格占比 |
| 全相等读数 | `s4_all_equal` | 0/0 不可判定 → 保留交互；分量全 0；`OK_PARTIAL`，贡献率/标准差比例/ndc 标为不可定义，规格占比为 0 |
| 拒绝后修正重算 | `s5_reject_then_fix` | 重复行 → `INVALID_INPUT`；缺格 → `INCOMPLETE_DESIGN`；修正后 `OK` |
| 各类非法输入 | `s6_invalid_inputs` | 未声明人员/零件、非有限读数、不兼容单位、规格倒置 → 均 `INVALID_INPUT` |

### s1 演算（手工核算值，与生产结果逐项一致）

格均值：P1×A=10.01，P1×B=10.05，P2×A=10.10，P2×B=10.24，P3×A=9.81，P3×B=9.85；
零件均值 10.03 / 10.17 / 9.83，操作员均值 9.973333 / 10.046667，总均值 10.01。

| 项 | SS | df | MS |
| --- | --- | --- | --- |
| 零件 | 0.2336 | 2 | 0.1168 |
| 操作员 | 0.01613333 | 1 | 0.01613333 |
| 交互 | 0.00666667 | 2 | 0.00333333 |
| 重复误差 | 0.0012 | 6 | 0.0002 |
| 合计 | 0.2576 | 11 | — |

F = 0.00333333/0.0002 = 16.667，F(2,6) 尾概率 p ≈ 0.00355 < 0.25 → 保留交互。
方差分量：σ²_e=0.0002，σ²_交互=(0.00333333−0.0002)/2=0.00156667，
σ²_操作员=(0.01613333−0.00333333)/6=0.00213333，
σ²_零件=(0.1168−0.00333333)/4=0.02836667。
GRR=0.0039，TV=0.03226667；方差贡献率 GRR 12.09% / 零件 87.91%；
6σ 标准差比例 GRR 34.77%；规格占比 46.84%（规格宽 0.8）；ndc=⌊1.41×0.168424/0.062450⌋=3。

### s5 的拒绝与修正

```bash
D=grr_demo/s5_reject_then_fix
# 第 9 行与第 8 行定位到同一测量格（H2×OP-B×第1次）→ 拒绝（退出码 4）
.venv/bin/python -I -m mes_pipeline.cli --store $S grr-run --study $D/study.json \
    --observations $D/observations_duplicate.csv --calc-id demo-s5-dup
# H3×OP-B 缺第 2 次重复（缺格）→ 拒绝（退出码 6）
.venv/bin/python -I -m mes_pipeline.cli --store $S grr-run --study $D/study.json \
    --observations $D/observations_missing_cell.csv --calc-id demo-s5-miss
# 修正后重算 → OK（退出码 0）
.venv/bin/python -I -m mes_pipeline.cli --store $S grr-run --study $D/study.json \
    --observations $D/observations_fixed.csv --calc-id demo-s5-fixed
```

### s6 的非法输入变体（共用 `observations_ok.csv` 为对照）

```bash
D=grr_demo/s6_invalid_inputs
for v in undeclared_operator undeclared_part non_finite bad_unit; do
  .venv/bin/python -I -m mes_pipeline.cli --store $S grr-run \
      --study $D/study.json --observations $D/observations_$v.csv --calc-id demo-s6-$v
done
.venv/bin/python -I -m mes_pipeline.cli --store $S grr-run \
    --study $D/study_bad_spec.json --observations $D/observations_ok.csv \
    --calc-id demo-s6-bad-spec
```

## 3. 独立复核

`verify.py` **不调用生产分析函数**：自行解析输入，用普通 Python 重算
平方和、自由度、均方、F 检验（仅 F 分布尾概率用 SciPy）、方差分量与全部
指标，再与生产系统保存的结果逐项比对：

```bash
# 先按上文跑出演示结果（demo-s1 … demo-s5-fixed），然后：
.venv/bin/python -I grr_demo/verify.py verify-all --store $S

# 单项：
.venv/bin/python -I grr_demo/verify.py verify-compute --store $S --calc-id demo-s1
.venv/bin/python -I grr_demo/verify.py verify-invariance --store $S
```

- `verify-compute`：逐项核对 SS/df/MS、F 与 p、分支决策、方差分量
  （raw/used/截断清单）、GRR/总变差、贡献率、6σ 比例、规格占比、ndc、
  分组均值与逐行定位；
- `verify-invariance`：对 s1 施加四种等价变换后经生产 CLI 重算并比对——
  交换零件/人员名字、打乱行序、整体平移 +0.5、mm→μm 换算
  （方差应 ×10⁶、标准差 ×10³、比例类指标不变）。

## 4. 计算口径与适用前提

**适用前提**：本入口只接受完整的平衡交叉设计——每个零件×操作员组合有
相同次数（≥2）的重复测量，至少 3 个零件、2 名操作员；模型为双因素
**随机效应**（零件与操作员均视为从更大总体中抽样），读数相互独立。
非平衡设计、嵌套设计、固定操作员效应、单次测量（repeats=1）都不适用，
本入口直接拒绝而不是给出近似结论。

**统计口径**：

1. 带零件×操作员交互的随机效应双因素 ANOVA；总平方和分解为
   零件+操作员+交互+误差四项并在结果中核对。
2. 交互项 F 检验以误差均方为分母（SciPy F 分布尾概率）；`p < alpha`
   保留交互，否则把交互的平方和与自由度合并入误差后重新估计。
   零分母规则：分母分子同为零 → 不可判定，保守保留交互；
   分母为零而分子为正 → F 发散，按显著交互处理（不产出 NaN 后继续比较）。
3. 方差分量由期望均方反解：保留交互时以交互均方为比较项
   （σ²_零件=(MS_P−MS_PO)/(b·n) 等），合并后改用合并误差均方。
4. **方差截断口径**：负的方差分量原估计保留在 `raw` 中供解释；进入
   GRR/总变差/比例指标前**各自独立截到零**（`used`），不按比例分摊、
   不把负值转嫁给其他分量。
5. 重复性=误差方差，再现性=操作员+保留交互的方差，GRR=两者之和，
   总变差=GRR+零件方差。贡献率是**方差**占比；6σ 口径的 %Study Var 是
   **标准差**占比（6 已约去）；规格占比=100×6σ_GRR/规格宽度；
   ndc=⌊1.41×σ_零件/σ_GRR⌋（1.41 为指定系数）。两种比例口径不混用。
6. 分母为零的比值（如全相等读数使总方差为零）在结果中明确标注
   `undefined` 及原因，其余计算完整保存（`OK_PARTIAL`），
   不写成 0、无穷或可用结论；无规格时不计算规格占比；
   规格宽度不正（upper ≤ lower）直接拒绝。
7. 长度支持 mm 与 μm 换算（1 mm = 1000 μm）；结果一律以试验声明的
   量纲报告；行的排列不改变统计结果（求和采用与顺序无关的
   `math.fsum`）。
