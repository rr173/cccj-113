# MES 流程分析管线

从 MES 导出包的原始事件行，到产线分析人员可用的流程分析表（直接跟随计数、路径
变体、等待时长分位数、工艺约束违规明细）的完整 Python 管线。轨迹统计由 PM4Py
计算，同时用一个**独立的小型状态机**逐行解释每批轨迹并交叉核对，两者结果断言一致。

## 1. 目录结构

```
mes_pipeline/
├── config.py          # 显式声明的产线时钟（格式+固定UTC offset）与工艺有向图规范
├── models.py          # 不可变值对象（原始事件/校准事件/合成批次/违规/等待样本）
├── ingest.py          # 入口①导入 ②封存：只读解析、UTC校准、哈希封存、封存集合
├── linking.py         # 交接条码关联（并查集）、未归属判定、撤回、条码修正
├── state_machine.py   # 独立小型状态机：逐行解释轨迹、悬空操作、违规、等待样本
├── analysis.py        # DFG/变体/分位数/违规表 + PM4Py 适配器与双路交叉核对
├── pipeline.py        # 门面：导入/封存/重算/查询四入口 + 版本化报告
├── cli.py             # 命令行入口（流程分析 + 配料计算）
└── charge/            # 配料计算（与 MES 活动轨迹无关，只用质量/数量数据）
    ├── models.py      # 库存批/化验区间/炉次要求等值对象，pct/ppm 统一为质量分数
    ├── io.py          # 输入读取与校验：缺证书/冲突/负数/单位不明/缺化验一律拒绝
    ├── solver.py      # 纯 Python 两阶段单纯形 + 分支定界 MILP + 冲突约束集(IIS)
    └── service.py     # 建模求解、保守口径核算、结果落盘与读取
└── gage/              # 量具 R&R（交叉型；只用零件×操作员×重复测量试验数据）
    ├── models.py      # 试验定义/观测行值对象，mm/μm 统一换算到试验量纲
    ├── io.py          # 输入校验：重复行/未声明/缺格/非有限/单位不兼容均拒绝
    ├── anova.py       # 随机效应双因素 ANOVA（带交互）+ 方差分量 + GRR 指标
    └── service.py     # 载入试验、计算、结果落盘与读取
└── lineage/           # 实耗批次谱系（只用外购批声明与实际领料/产出记录；纯标准库）
    ├── models.py      # 质量值对象（Fraction 精确算术，g/kg 内部统一为克）
    ├── io.py          # 装载校验：引用/重复/守恒/成环/累计超领定位到原记录后整体拒绝
    ├── engine.py      # 拓扑排序、均匀混合传播、路径留痕、余额、召回、全局账
    └── service.py     # 装载、精确序列化、结果落盘/读取、构成查询与召回测算
synthetic/
├── generate.py        # 确定性人工合成材料（覆盖九类场景）
├── audit_demo.py      # 状态机逐行审计演示
└── deps_generate.py   # 供货属性演示小表 + 12 列×20000 行规模样本
charge_demo/           # 配料计算演示材料（七个场景）+ 独立复核脚本 verify.py
grr_demo/              # 量具 R&R 演示材料（六个场景）+ 独立复核脚本 verify.py
lineage_demo/          # 实耗批次谱系演示材料（五个场景）+ 独立复核脚本 verify.py
deps_demo/             # 依赖发现/规则核查演示材料（八个场景）+ 独立复核脚本 verify.py
tests/                 # 状态机/关联/端到端 + 配料 + 量具 R&R + 谱系测试
```

## 2. 关键业务规则的落地方式

| 需求 | 实现 |
| --- | --- |
| `batch_label` 仅产线内唯一，跨系统只认交接条码 | `linking.py` 先按 `(line_code, batch_label)` 分组，再用条码做并查集并批，绝不做字符串近似配对 |
| 条码缺失 / 多对一 | 缺失 → `MISSING_BARCODE`；同一产线内多批次共用条码 → `AMBIGUOUS_BARCODE`，相关批次全部列为未归属，不自动猜测 |
| 原始导出不可变 | 封存只做字节复制 + SHA-256；源目录哈希前后一致（有测试）；同 ID 内容变化再封存直接拒绝 |
| 撤回单按 `source_event_id` 指向记录并保留撤回单号 | `apply_withdrawals` 逻辑删除，撤回单号进入 `link_meta`；指向不存在记录/重复指向均报错 |
| 每条产线独立时钟格式与固定 UTC offset | `config.json` 显式声明，解析即校准为 UTC；未声明产线直接报错 |
| 同刻事件稳定排序 | 排序键 `(utc_time, row_number, source_event_id)` |
| OPEN/CLOSE 是不同 action，未配对为悬空 | 状态机用操作栈配对；残留 OPEN 记 `OPEN_WITHOUT_CLOSE`，无 OPEN 的 CLOSE 记 `CLOSE_WITHOUT_OPEN`；**悬空不估算加工时长** |
| 前序边/并行/可选/返工边 | 有向图 `edges` + `rework_edges` + `optional`；OPEN 前校验非可选前序已完成；返工边允许重入且不计阻断 |
| 违规列出阻断边与原始行位置 | `Violation.blocking_edge` + `row_number/package_id/source_event_id` 全保留 |
| 只纳入已封存集合 | `sealed_sets/<id>.json` 显式列包，纳入前逐包哈希校验 |
| 撤回/修正后按关联批次重建，旧报告可读 | `recompute` 整体重算并按 `version_id` 落盘，历史版本永久保留 |

**等待时长定义**：某操作 OPEN 时刻 − 轨迹中上一操作 CLOSE 时刻（秒）。轨迹首个
操作无前驱不产生样本；悬空操作不参与加工时长估算。

## 3. 安装与测试

```bash
python3 -m venv .venv --without-pip          # 系统无 ensurepip 时
.venv/bin/python get-pip.py
.venv/bin/python -m pip install -e . pytest
.venv/bin/python -I -m pytest tests/ -q      # 129 passed
```

`-I` 隔离模式下仍可导入，因为项目以 editable 方式安装（pth 位于 site-packages）。

## 4. 流程分析的四个交付入口（CLI）

```bash
# 生成合成材料
.venv/bin/python -I -m synthetic.generate --root data/raw

# ① 导入（只读解析，不修改源目录）
.venv/bin/python -I -m mes_pipeline.cli --config data/raw/config.json --store data/store \
    import --package data/raw/pkg-line-a

# ② 封存（复制 + 哈希）并声明封存集合
for p in pkg-line-a pkg-line-b pkg-admin; do
  .venv/bin/python -I -m mes_pipeline.cli --config data/raw/config.json --store data/store \
      seal --package data/raw/$p
done
.venv/bin/python -I -m mes_pipeline.cli --config data/raw/config.json --store data/store \
    seal-set --set-id S1 --package pkg-line-a --package pkg-line-b --package pkg-admin

# ③ 重算（应用撤回单/条码修正，版本化报告；内含 PM4Py 双路核对）
.venv/bin/python -I -m mes_pipeline.cli --config data/raw/config.json --store data/store \
    recompute --set-id S1 --version v1

# ④ 轨迹查询（按合成批次 ID 或产线批次）
.venv/bin/python -I -m mes_pipeline.cli --config data/raw/config.json --store data/store \
    query --set-id S1 --line LINE-A --label B-REWORK
.venv/bin/python -I -m mes_pipeline.cli --config data/raw/config.json --store data/store \
    report --set-id S1 --version v1     # 旧版本报告仍可读取
```

Python API 入口对应 `mes_pipeline.pipeline.Pipeline` 的
`import_package / seal / create_sealed_set / recompute / read_report / query_trace`。

## 5. 配料计算（charge）

在**开炉前**根据原料库存、化验结果与炉次要求求最低成本投料方案。求解输入只有
质量/数量数据（库存表、化验表、证书登记表、炉次要求），**不使用 MES 活动轨迹**。

```bash
# 求解（退出码：0 最优 / 3 约束无解 / 4 输入不合法 / 5 未收敛）
.venv/bin/python -I -m mes_pipeline.cli --store data/store charge-solve \
    --inventory charge_demo/s1_bulk_optimum/inventory.csv \
    --assays    charge_demo/s1_bulk_optimum/assays.csv \
    --certs     charge_demo/s1_bulk_optimum/certificates.csv \
    --heat      charge_demo/s1_bulk_optimum/heat.json \
    --calc-id demo-s1

# 查看某次计算结果（成功与失败结果都会落盘，均可读取）
.venv/bin/python -I -m mes_pipeline.cli --store data/store charge-show --calc-id demo-s1
```

计算口径：

- 散装批按连续重量取用；整桶批必须整桶（净重整数倍），不足一桶的库存不可用；
- 化验值一律按闭区间处理：单值 `[v,v]`、上下界 `[lo,hi]`、`<x` 为 `[0,x]`（不取中点）；
  pct 与 ppm 统一为质量分数后求解；
- 成分不确定性按保守口径：最低含量用各原料下界核算、最高含量用上界核算，
  保证区间内任何真实成分都不突破炉次要求，不把区间收缩成平均数；
- 缺证书、同批同元素相互冲突的有效结果、负数、单位不明、受约束元素缺化验
  一律拒绝计算（`INVALID_INPUT`），缺值不按零填充；
- 库存上限、整桶整数约束、互斥关系与质量守恒同时满足；目标为总投料费用最小，
  多最优时给出各批次在最优费用下的投料区间说明等价性；
- 无可行方案时输出极小冲突约束集（整体不可行、移除任意一条即可行，
  可用 `charge_demo/verify.py verify-iis` 重新求解逐条验证），不自动放宽限制。

求解器为自研纯 Python（两阶段单纯形 + 分支定界），**不新增运行时第三方依赖**；
正确性另有 scipy/穷举对照（开发期一次性验证，不进交付依赖）。
演示材料与计算口径详见 `charge_demo/README.md`。

## 6. 量具 R&R（gage，交叉型）

以**零件×操作员×重复测量**的试验数据为对象，把量具本身的波动（重复性）、
操作员差异（再现性）与零件真实差异分开。输入为试验定义（`study.json`）与
观测行（`observations.csv`），**不从工序轨迹或配料计算结果中推导样本**。

```bash
# 计算并保存（退出码：0 完成 / 4 输入不合法 / 6 设计不完整）
.venv/bin/python -I -m mes_pipeline.cli --store data/store grr-run \
    --study grr_demo/s1_interaction_kept/study.json \
    --observations grr_demo/s1_interaction_kept/observations.csv \
    --alpha 0.25 --calc-id demo-s1

# 按试验 ID 查看该次结果（成功/部分成功/失败均可读）
.venv/bin/python -I -m mes_pipeline.cli --store data/store grr-show --calc-id demo-s1
```

计算口径：

- 只接受完整平衡交叉设计（≥3 零件、≥2 操作员、每格 ≥2 次重复）；
  重复行/未声明人员或零件/缺格/非有限读数/不兼容单位说明位置并拒绝；
- 带零件×操作员交互的随机效应双因素 ANOVA；交互检验以误差均方为分母，
  `p < alpha` 保留交互，否则合并入误差重新估计；0/0 不可判定保守保留，
  分母为零分子为正按显著处理；
- 负方差分量保留原估计（`raw`），进入指标前各自独立截到零（`used`）；
- 输出方差贡献率与 6σ 标准差比例（两种口径不混用）、规格占比与
  ndc=⌊1.41·σ_零件/σ_GRR⌋；分母为零的比值明确标注不可定义及原因；
- 长度支持 mm/μm 换算；行的排列不改变统计结果。

演示材料、手工演算与独立复核（`grr_demo/verify.py`，不调用生产分析函数）
详见 `grr_demo/README.md`。

## 7. 实耗批次谱系（lineage）

供应商通知某批原料有问题时，本模块回答“它已经混进哪些**在库物料**、各还剩
多少”，而不是只给一串相连批次号。输入只有两份**实际记录**——外购原料批
声明（批次、物料、初始质量）与加工操作（operation_id、实耗输入批及质量、
新产出批及质量、损耗质量）。**不从 MES 工序轨迹、配料建议或量具试验推测
实际用料**；子系统为纯标准库实现，无新增第三方依赖。

```bash
S=/tmp/line_store
# 装载并计算（退出码 0 成功 / 4 输入不合法；失败也落盘为 INVALID_INPUT）
python3 -I -m mes_pipeline.cli --store $S lineage-load \
    --materials lineage_demo/s3_diamond/materials.json \
    --operations lineage_demo/s3_diamond/operations_reverse.json \
    --calc-id demo-s3
# 构成查询：任何已知批的生成/剩余质量与按外购批拆分的构成（含流经操作路径）
python3 -I -m mes_pipeline.cli --store $S lineage-composition \
    --calc-id demo-s3 --batch F-1
# 召回测算：多个可疑外购批，输出只列正余额且含可疑原料的在库批
python3 -I -m mes_pipeline.cli --store $S lineage-recall \
    --calc-id demo-s3 --suspect RM-X --suspect RM-Y
```

计算口径：

- 全部质量以有理数（`fractions.Fraction`）精确运算，单位支持 g/kg 混写；
  质量守恒与累计超领判定**无浮点容差**，超领一克也会被定位拒绝，非整数
  JSON 浮点输入直接拒绝（要求十进制字符串）；
- 记录顺序不代表加工顺序：先按生产者/消费者建操作依赖图、拓扑排序后计算；
  引用不存在的批、同批在一次操作内重复列出、重复 operation_id/产出批双
  生产者、覆盖外购批、循环依赖、累计超领都定位到原记录并**整体拒绝**本次
  装载，不跳过坏操作继续给谱系；修正文件后可重新装载，旧结果仍可查询；
- **理想均匀混合假设**：批内组成处处相同，部分领用按各原料占比分摊；同一
  操作的各产出与损耗按全部投入的混合组成等比例分配，损耗只减量不选择性
  去除原料；未领用部分保持原组成；
- 菱形路径上同一原料沿各支的贡献**相加**，每条贡献保留完整批链/操作链
  路径依据，不去重丢支、不把整支祖先量重复算两遍；
- 召回逐批给出可用质量、可疑原料质量与占比、应隔离的整批质量及路径依据；
  完全耗尽的中间批不计入隔离总量，同批含多种可疑原料只隔离一次；并按可疑
  原料核对“初始质量 = 所有批剩余贡献 + 累计损耗贡献”的全局账；
- 除不尽的占比展示为 28 位小数（HALF_UP）并附精确分数字段与
  `all_decimals_exact` 标志，内部核算始终精确。

演示材料、手工数值演算与独立复核（`lineage_demo/verify.py`，不调用生产
分析函数，用独立的 Fraction 参考实现逐项核对）详见 `lineage_demo/README.md`。

## 8. 合成材料覆盖的九类场景

1. 缺失交接条码（`B-MISS`）与同产线歧义条码（`B-AMB1/B-AMB2` 共用 `BC-DUP`）
2. OPEN 没有 CLOSE（`B-DANGLE` 的 CUT）
3. 同刻事件按导出行号稳定排序（`B-PAR`、`B-JOIN` 的并行 OPEN）
4. 产线固定时差与不同时钟格式（LINE-A `%Y-%m-%d %H:%M:%S` +8；LINE-B `%d/%m/%Y %H:%M:%S` +1）
5. 并行步骤（`WELD-A`/`WELD-B` 共享前驱 CUT、汇聚到 INSPECT）
6. 合法返工（`B-REWORK` 经返工边 `INSPECT->CUT` 重入，无阻断违规，rework_count=1）
7. 缺少必需工序（`B-MISSREQ` 跳过 WELD-B，给出阻断边 `WELD-B->INSPECT` 与 MISSING_REQUIRED）
8. 撤回一条中间事件（`WD-0001` 撤回 `B-WD` 的 CUT CLOSE，CUT OPEN 随之变悬空）
9. 条码修正引发批次拆分（`BCFIX-0001` 后 `B-SPLIT` 与 `X-SPLIT2` 分离为两个合成批次）

## 9. PM4Py 与状态机的核对关系

- `analysis.build_pm4py_dataframe` 把批次事件展平为 PM4Py 事件表
  （`case_id/activity/timestamp`），`convert_to_event_log` 后用 DFG discovery 与
  variants filter 计算直接跟随计数与路径变体；
- 纯 Python 参考实现（`direct_follow_counts/path_variants`）独立计算同一指标；
- `cross_check_with_pm4py` 断言两路结果完全相等，不一致即抛 `AssertionError`，
  报告的 `cross_check` 段记录核对结论与轨迹/事件总数；
- 状态机的逐行转移可通过 `synthetic.audit_demo` 人工审计，每条违规都带原始行位置。

## 10. 单表字段依赖发现与规则核查（deps）

供应商物料属性宽表常常没有可靠主键。`deps` 只研究**一张表内部**字段值的对应
关系（不合实体、不看加工先后、不做统计估计），回答「供应商编码 + 零件号能否
认出一行」「同一零件号是否总对应同一名称」，并能对一条拟议规则直接给出反例。

```bash
# 生成演示材料（八个场景）与 12 列 × 20000 行规模样本
.venv/bin/python -I -m synthetic.deps_generate --root deps_demo --scenario demo
.venv/bin/python -I -m synthetic.deps_generate --root deps_bench --scenario bench

S=/tmp/deps_store
# 发现：1..k 全部列组合上的最小唯一键与最小函数依赖
.venv/bin/python -I -m mes_pipeline.cli --store $S deps-discover \
    --columns deps_demo/s1_composite_key/columns.json \
    --rows deps_demo/s1_composite_key/rows.jsonl \
    --k 3 --min-rows 2 --null-policy equal --calc-id demo-s1
# 核查任意键 / 任意单右部依赖（成立、违反或证据不足）
.venv/bin/python -I -m mes_pipeline.cli --store $S deps-check \
    --columns deps_demo/s2_null_policies/columns.json \
    --rows deps_demo/s2_null_policies/rows.jsonl \
    --k 3 --min-rows 2 --null-policy ignore --left supplier --right name
# 解释违反：加一列的核查结果 + 右部列真正的最小左部（原值不改）
.venv/bin/python -I -m mes_pipeline.cli --store $S deps-explain \
    --columns deps_demo/s4_violation_to_hold/columns.json \
    --rows deps_demo/s4_violation_to_hold/rows.jsonl \
    --k 3 --min-rows 2 --null-policy equal --left supplier --right name
```

口径要点：

- 值域只有字符串或 null，空字符串是实际值；前导零/大小写/空格按字面保留；
  少列、多列、重复 row_id、重复列名、其他值类型都**定位到行号与列名后整体拒绝**；
- `equal` 把 null 当作有别的共同值；`ignore` 对每条规则单独选行（键排除键列含
  null 的行，依赖排除左部或右部含 null 的行）；`min_rows` 参与数不足即证据不足；
- 每条结果写明参与数、排除数与口径；**成立**只对本资料/口径/门槛负责，不是永久承诺；
- 每条最小键/依赖附「删去任一左部列后」的核查摘要，说明更小组合为什么不够；
- 发现先在索引上做组合搜索（`groups(X∪{A}) == groups(X)` 精确剔除冲突），对
  需要定论的候选再逐行核实；覆盖全部组合、不抽样、不建全体行对；规模超限直接
  拒绝且不标注为完整；
- 反例固定取 `row_id` Unicode 序最小的行对，打乱资料顺序后结果与反例逐字节一致。

演示材料的独立复核脚本（不调用生产搜索，用逐对枚举重算）：
`.venv/bin/python -I deps_demo/verify.py verify-all`。详见 `deps_demo/README.md`。
