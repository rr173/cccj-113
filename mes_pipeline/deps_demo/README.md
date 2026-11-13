# 单表字段依赖发现与规则核查（deps）

供应商交来的物料属性宽表常常**没有可靠主键**。仓库同事关心的是：

- 「供应商编码 + 零件号」能不能认出一行？
- 同一个零件号是否一定对应同一个名称？
- 哪些规则值得写进**入仓校验**？

本子系统只回答**一张表内部字段值的对应关系**。它不合并实体、不分析加工先后、
不做统计估计，也不从流程轨迹或配料结果里推测任何东西。

## 0. 输入格式

资料由两份文件构成：

| 文件 | 说明 |
| --- | --- |
| `columns.json` | `{"table_id": "...", "columns": ["supplier", "part", "name", "grade", "package", "origin"]}` |
| `rows.jsonl` | 每行一条记录：`{"row_id": "R-001", "values": {"supplier": "S1", ...}}` |

**值的域只有字符串或 null**：`""` 是实际值；前导零、大小写与空格都按字面保留，
绝不 strip / lower / 数值化。列名同样按字面匹配。

校验规则（所有问题一次性收集，定位到**文件行号与列名**后整体拒绝）：

- 列清单缺 `columns` / 重复列名 / 列名不是非空字符串；
- 行缺 `row_id`、`row_id` 重复或为空；
- 行内缺列（少列）、出现列清单之外的列（多列）、同一 JSON 对象内重复键；
- 值不是字符串也不是 null（数字、布尔、数组、对象一律拒绝）。

## 1. 发现与核查入口

```bash
S=/tmp/deps_store
# ① 发现：最小唯一键 + 最小函数依赖（覆盖 1..k 全部列组合）
.venv/bin/python -I -m mes_pipeline.cli --store $S deps-discover \
    --columns deps_demo/s1_composite_key/columns.json \
    --rows deps_demo/s1_composite_key/rows.jsonl \
    --k 3 --min-rows 2 --null-policy equal --calc-id demo-s1

# ② 核查一条拟议的键（--key 可重复传入以表示组合键）
.venv/bin/python -I -m mes_pipeline.cli --store $S deps-check \
    --columns deps_demo/s1_composite_key/columns.json \
    --rows deps_demo/s1_composite_key/rows.jsonl \
    --k 3 --min-rows 2 --null-policy equal --key supplier --key part

# ③ 核查一条拟议的依赖（省略 --left 表示空左部常量依赖）
.venv/bin/python -I -m mes_pipeline.cli --store $S deps-check \
    --columns deps_demo/s2_null_policies/columns.json \
    --rows deps_demo/s2_null_policies/rows.jsonl \
    --k 3 --min-rows 2 --null-policy ignore --left supplier --right name

# ④ 解释一条违反的依赖：加一列的核查结果 + 右部列的真正最小左部
.venv/bin/python -I -m mes_pipeline.cli --store $S deps-explain \
    --columns deps_demo/s4_violation_to_hold/columns.json \
    --rows deps_demo/s4_violation_to_hold/rows.jsonl \
    --k 3 --min-rows 2 --null-policy equal --left supplier --right name

# ⑤ 查看已保存的结果（成功或失败都可读）
.venv/bin/python -I -m mes_pipeline.cli --store $S deps-show --calc-id demo-s1
```

退出码：`0` 完成 / `2` 结果不存在 / `4` 资料、参数或规则不合法 /
`7` 规模超出已声明范围。

## 2. 口径（必须写在结果里，也逐条输出）

| 口径 | 含义 |
| --- | --- |
| 唯一键 K | 任意两条参与行在 K 上的值元组都不同 |
| 函数依赖 X → A | A 是不在 X 中的单列；两行 X 相同则 A 必须相同；X 允许为空（常量列） |
| `equal` | null 视为一种有别于任何字符串的共同值，所有行参与 |
| `ignore` | **每条规则单独选行**：唯一键排除 K 含 null 的行；X → A 排除 X 或 A 含 null 的行 |
| `min_rows` | 参与行数低于它即**证据不足**，不能列为成立（≥2） |

- **最小性**只相对于同口径、同 `min_rows` 下**证据充足且成立的真子集**判断；
- 结果逐条写明 `participating_rows` / `excluded_rows` / 排除原因与口径；
- **空表或只有一条资料**不宣称发现键或依赖（`claimable=false`）；
- 「没有反例」与「资料足够支持」分开记录：无冲突但参与行不足的规则进入
  `evidence_gaps`，明确标 `INSUFFICIENT_EVIDENCE`，绝不混进成立结果；
- 每条最小键/依赖都附 `minimality`：删除其任一左部列后的核查摘要与反例，
  说明**更小组合为什么不够**（空左部依赖没有待删列）。

### 关键实现口径：唯一键与「常量列」

唯一键定义要求**非空**；空组合不是键。常量列（`∅ → A`）同样意味着该列只有
一个值，因此它在参与行上两两相同——是**相反**于键的条件，不会被列为键。
单列键删除到空组合时没有可用反例，该摘要标为 `NOT_APPLICABLE`，如实说明
「没有更小组合可核查」，不伪造核查结论。

### 反例的确定性

违反时返回一对实际 `row_id` 及字段值，且始终取按 `row_id` 的 **Unicode 码位序**
排序后的最小行对，与输入文件里的物理顺序无关。**打乱资料顺序**后，发现集合
与反例逐字节一致（复核脚本 `verify-shuffle` 专门检查）。

## 3. 搜索方法与规模

- **一次扫描 → 列等值索引**：按 `row_id` 排序、每列值映射成紧凑整数码（null
  单独一码），保留非空行位图；原始字符串只存一份。搜索与核查都在这份索引上
  进行，不重复打开文件，也不构造全体行对。
- **组合搜索**：按左部大小逐层枚举 **1..k 的全部列组合**（BFS），组合标签
  （等值类 ID）由父层标签与新增列码合成。唯一键 = 组合层内标签数等于参与行数；
  函数依赖先用「`groups(X∪{A}) == groups(X)`」这一**必要条件**精确剔除有冲突
  的候选（不会被哈希碰撞骗过），再对需要判定最小性/成立性的候选做一次**逐行
  精确核实**（凑到冲突即停）。不是抽样，也不会输出只搜索了一部分的「完整」。
- **规模上限**（超出即 `status=SCALE_EXCEEDED` 拒绝，绝不标注为完整）：
  列数 ≤ 32、行数 ≤ 200000、`1..k` 的组合数 ≤ 200000、单元格数 ≤ 20000000。
- 核查任意指定规则时按索引逐行扫描；反例取最小 `row_id` 对。

规模样本实测（`deps_bench/`，12 列 × 20000 行，`k=3`、`min_rows=2`，
本机 Python 3.11 / 单进程 `-I`）：

| 口径 | 端到端耗时（含载入+建索引） | 最高常驻内存（`ru_maxrss`） | 覆盖组合数 |
| --- | --- | --- | --- |
| `equal` | 4.2–4.3 秒 | 232.5 MB | 298 / 298 |
| `ignore` | 16.97 秒 | 248.5 MB | 298 / 298 |

最高内存取进程整个生命周期的 `ru_maxrss`（含解释器与已装载依赖）；纯引擎
（复用同一份索引跑两种口径）的峰值约 70 MB。`ignore` 更慢，是因为 `null` 会按
规则筛行、需要更多次的按口径重算组数。
复现命令：

```bash
.venv/bin/python -I -m synthetic.deps_generate --root deps_bench --scenario bench
.venv/bin/python -I -m mes_pipeline.cli deps-bench \
    --columns deps_bench/columns.json --rows deps_bench/rows.jsonl \
    --k 3 --min-rows 2 --null-policy equal
```

`deps-bench` 输出 `elapsed_seconds` 与 `peak_memory_mb`（`ru_maxrss`，Linux 为
KiB，取进程整个生命周期的最高值，包含解释器与依赖本身）。

## 4. 演示材料（`deps_demo/`）

```bash
.venv/bin/python -I -m synthetic.deps_generate --root deps_demo --scenario demo
```

（`deps_demo/` 里的演示材料已随仓库提供，无需生成即可直接运行下面的命令；
`--root` 指向新的空目录可重新生成一份。）

| 场景 | 覆盖要点 |
| --- | --- |
| `s1_composite_key` | **两列合起来唯一、各单列都不唯一**；含空左部常量依赖 `∅ → origin` |
| `s2_null_policies` | 某列含 null，`supplier → name` 在 `equal` 下违反、`ignore` 下成立 |
| `s3_evidence_gap` | `ignore` 下 `supplier → name` 成立；`batch → name` 无冲突但参与仅 1 行 → **证据不足**（`evidence_gaps`，不列为成立） |
| `s4_violation_to_hold` | `supplier → name` 违反，加 `part` 后成立；原值一字未改 |
| `s5_bad_rows` | 少列 / 多列 / 重复 row_id / 值类型错误，定位到行与列 |
| `s6_bad_rows_fixed` | 同一份资料修好后的版本，重跑成功 |
| `s7_typo_columns` | 列清单重复列名 |
| `s8_duplicate_rows` | 含整行重复（row_id 不同）：在 k 范围内找不到任何唯一键 |

结果文件里 `column_groups` 默认只给每列的摘要（取值数、null 行数、最大的几个
取值）；需要逐值 `row_id` 明细时加 `--include-column-rows`，大表的结果文件不会
被解释性数据撑大。

## 5. 独立复核（`deps_demo/verify.py`）

复核脚本**不调用**生产搜索：自行解析输入，用逐对枚举实现参与行选择、冲突判定
与反例取对，再与生产结果逐项比对（集合、参与数），并专门检查：

```bash
.venv/bin/python -I deps_demo/verify.py verify-all --store /tmp/deps_verify_store
```

- 全部场景的最小键 / 最小依赖集合与独立枚举器一致；
- `s2` 在同一资料上两种口径结论相反；
- `s3` 的「无冲突但证据不足」确实存在且参与行数为 1；
- 每张表随机打乱后，发现集合与反例完全一致。

测试里另有一个更小的枚举器 `mes_pipeline.deps.bruteforce`，在随机小表上与生产
引擎逐项比对（`tests/test_deps_engine.py`），保证完备性不依赖抽样或哈希碰撞的
侥幸。

## 6. 局限

- 只研究一张表内字段值的精确对应关系；不做实体合并、不分析加工先后、不做统计
  估计。
- **成立**仅指「本资料 + 当前口径 + 当前 `min_rows` 下证据充足且未发现反例」，
  不是供应商的永久承诺：换一批资料、换口径或提高 `min_rows` 都可能改变结论。
- 唯一键与函数依赖只覆盖长度不超过 `k` 的组合；需要更长组合时请提高 `k`
  （受规模上限约束）。
