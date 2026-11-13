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
└── cli.py             # 命令行入口
synthetic/
├── generate.py        # 确定性人工合成材料（覆盖九类场景）
└── audit_demo.py      # 状态机逐行审计演示
tests/                 # 32 个测试（状态机单测 + 关联单测 + 端到端）
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
.venv/bin/python -I -m pytest tests/ -q      # 32 passed
```

`-I` 隔离模式下仍可导入，因为项目以 editable 方式安装（pth 位于 site-packages）。

## 4. 四个交付入口（CLI）

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

## 5. 合成材料覆盖的九类场景

1. 缺失交接条码（`B-MISS`）与同产线歧义条码（`B-AMB1/B-AMB2` 共用 `BC-DUP`）
2. OPEN 没有 CLOSE（`B-DANGLE` 的 CUT）
3. 同刻事件按导出行号稳定排序（`B-PAR`、`B-JOIN` 的并行 OPEN）
4. 产线固定时差与不同时钟格式（LINE-A `%Y-%m-%d %H:%M:%S` +8；LINE-B `%d/%m/%Y %H:%M:%S` +1）
5. 并行步骤（`WELD-A`/`WELD-B` 共享前驱 CUT、汇聚到 INSPECT）
6. 合法返工（`B-REWORK` 经返工边 `INSPECT->CUT` 重入，无阻断违规，rework_count=1）
7. 缺少必需工序（`B-MISSREQ` 跳过 WELD-B，给出阻断边 `WELD-B->INSPECT` 与 MISSING_REQUIRED）
8. 撤回一条中间事件（`WD-0001` 撤回 `B-WD` 的 CUT CLOSE，CUT OPEN 随之变悬空）
9. 条码修正引发批次拆分（`BCFIX-0001` 后 `B-SPLIT` 与 `X-SPLIT2` 分离为两个合成批次）

## 6. PM4Py 与状态机的核对关系

- `analysis.build_pm4py_dataframe` 把批次事件展平为 PM4Py 事件表
  （`case_id/activity/timestamp`），`convert_to_event_log` 后用 DFG discovery 与
  variants filter 计算直接跟随计数与路径变体；
- 纯 Python 参考实现（`direct_follow_counts/path_variants`）独立计算同一指标；
- `cross_check_with_pm4py` 断言两路结果完全相等，不一致即抛 `AssertionError`，
  报告的 `cross_check` 段记录核对结论与轨迹/事件总数；
- 状态机的逐行转移可通过 `synthetic.audit_demo` 人工审计，每条违规都带原始行位置。
