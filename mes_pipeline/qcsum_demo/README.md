# 质检仓库增量关系汇总（qcsum）

质检仓库的汇总表不能只靠追加：检测记录会撤销、样本会改挂批次、批次的供应商
归属会更正。本模块把 `lots LEFT JOIN samples` 按 `(supplier, site)` 分组的汇总
**持久化在 SQLite 里**，更正到来时只处理受影响的关联记录做增量维护，不重做全表
连接；原表、辅助计数、汇总与成功账在同一事务中生效。

- 生产代码：`mes_pipeline/qcsum/`（纯标准库，无新增第三方依赖）
- 演示材料与核对：`qcsum_demo/`（本目录）
- 测试：`tests/test_qcsum_*.py`

## 1. 汇总口径（固定）

```
lots LEFT JOIN samples ON lots.lot_id = samples.lot_id，按 (supplier, site) 分组：
  joined_rows   连接结果行数（无样本批次贡献一条占位行：max(该批样本数,1) 求和）
  n_samples     实际样本数（同批多样本各算一条；不是批次数）
  n_lots        不同批次数
  n_grades      grade 的不同非 null 值数量（空字符串是有含义的值，算一个）
  rejected_sum  rejected_units 之和；全部数量为 null（或无样本）时为 null，
                有一个明确的 0 时和为 0
```

组内最后一个批次消失时整组删除，不留全零空组。样本允许引用尚未到达或已被删除
的批次（孤立样本留在原表，不进任何组，批次出现后接回）；不设外键、不设级联删除。

## 2. 输入约定（违反一律拒绝）

- 批次：`{"batch_id": <非空字符串>, "ops": [...]}`，必须且只能有这两个键；
- 操作：`{"op": insert|update|delete, "table": lots|samples, "key": <主键>}`，
  insert/update 另有 `row`（完整行，主键必须等于 key，主键本身不可改），
  delete 不得带 `row`；不多不少恰好这些字段；
- 行：恰好本表全部列，缺列、多列拒绝；lot_id/sample_id/supplier/site 为非空
  字符串按原文相等；grade 为字符串或 null（空串有含义）；rejected_units 为
  0..1000000 的整数或 null，**布尔值不当整数**；
- JSON 任何层级的**重复对象键**拒绝（`loads_strict`）；
- 对象键次序不算改内容；**操作列表次序有意义**（存在性按列表依次检查，
  同批可反复改同一键、可先删后插）；
- insert 要求键不存在，update/delete 要求键存在；任一操作错误，整批回滚，
  修正后同一 batch_id 可继续使用；
- 已成功批次原内容重送只返回既有结论（`status=replayed`），不重复加减；
  同号改内容抛 `QcsumConflict`（内容按规范化 JSON 的 SHA-256 比较）。

## 3. 增量状态设计

### 3.1 表与角色

| 表 | 角色 |
| --- | --- |
| `lots` / `samples` | 原表；`samples(lot_id)` 上有关联索引 |
| `qc_sample_agg(lot_id, n_samples, rej_sum, rej_nonnull)` | 每个**被样本引用的** lot_id 的样本侧聚合；批次不存在也保留（孤立样本的接回依据） |
| `qc_sample_grade_agg(lot_id, grade, n)` | 每个 lot_id 每个非 null grade 的样本数 |
| `qc_grade_counts(supplier, site, grade, n)` | 每组每个非 null grade 的在组样本数，n>0 才保留行 |
| `qc_summary(supplier, site, joined_rows, n_samples, n_lots, n_grades, rejected_sum, rej_nonnull)` | 持久汇总；`rejected_sum` 就是答案列（全 null 时为 NULL），`rej_nonnull` 是辅助列；n_lots≥1 才保留行 |
| `qc_batches(batch_id, batch_hash, result_json, applied_at)` | 成功账 |

不变量：对每个现存批次 L（所在组 G）——`qc_summary[G]` 的各项等于组内各批
`qc_sample_agg` 的对应聚合（joined_rows 为 `max(k,1)` 之和）；`qc_grade_counts[G,g]`
等于组内各批 `qc_sample_grade_agg[L,g]` 之和；`n_grades` 等于该组
`qc_grade_counts` 行数。删除路径是同一组等式的负 delta，因此**每条 delta 都可逆**，
先删后插同键自然净零。

### 3.2 各类操作的增量规则

- **样本增/删/改**：先维护样本侧聚合 `qc_sample_agg`/`qc_sample_grade_agg`
  （无论批次是否存在，O(1)）；批次存在时把 delta 传播到所在组：
  `joined_rows += max(新k,1) - max(旧k,1)`（首条样本到达时占位行变真实行，
  行数不变；末条样本离开时重新出现占位行），`n_samples`/和/非空数同步加减，
  grade 经 `qc_grade_counts` 0↔正跳变调整 `n_grades`。改 = 旧值负 delta +
  新值正 delta，样本换批次自然跨组迁移。
- **批次建行/删除/换组**：按该批自己的 `qc_sample_agg` 与
  `qc_sample_grade_agg` 整批从旧组减去、加到新组——只读**该批次的样本邻域**
  （1 行样本聚合 + 该批不同 grade 行数），不重扫 samples 表，因此批次换组与
  同批样本增删可以任意次序组合而不漏算、不重复扣减。删除批次时样本侧聚合
  保留，孤立样本仍在原表；重建同键批次时按聚合接回。
- **不同 grade 数**：只维护每 (组, grade) 的准确计数行，从 0 变正 +1、变 0
  删行 -1；不把不同值集合序列化进一个字段反复整组扫描，也不用估计摘要。
- **rejected_sum 的 null 口径**：`qc_summary.rejected_sum` 保存真实答案；
  辅助列 `rej_nonnull` 记录非空数量数，归零时把和置回 NULL，出现明确 0 时
  和为 0。
- **空组**：`n_lots` 归零即删除整行（删除前断言其余计数已归零，属内部不变量
  保护），不留全零空组。

### 3.3 复杂度

每批代价 = O(操作数 + 涉及批次的样本邻域 + 涉及的 (组,grade) 键)，全部走主键
与 `samples(lot_id)`、`qc_*` 主键索引；**不**在每批末尾做全表连接、不删除重建
全部汇总、不读取未受影响分组重算 distinct。唯一一次全量扫描是
`load_initial`（用 SQL 聚合一次性建立全部辅助状态）。

## 4. 事务、成功账与故障注入

- 每批的原表、辅助计数、汇总、成功账在同一个 `BEGIN IMMEDIATE … COMMIT`
  中生效；任一操作错误整体 ROLLBACK，四者都保持批前状态；
- 成功账记录 `batch_id → 内容哈希 + 既有结论(含统计)`；重送同内容直接返回
  既有结论，同号异内容拒绝；
- 可重复故障注入点（`apply_batch(..., crash_at=…)` 或 CLI `--crash-at`）：
  - `after_base`：原表已改、辅助状态未改完时 `os._exit(137)` 真实终止进程
    （事务未提交）；新进程只能看见完整旧状态，该批可重送；
  - `after_commit`：提交后、返回前真实终止；新进程只能看见完整新状态，
    重送该批返回既有结论、只生效一次；
  - 故障演练不改库内容伪装崩溃，就是真的杀死进程（WAL 保证未提交事务回滚）。

## 5. 运行统计（复查处理范围）

`apply_batch` 返回的 `stats` 字段及计数位置（均在 `store.py` 对应助手内自增）：

| 字段 | 含义 | 计数位置 |
| --- | --- | --- |
| `lots_touched` | 受影响批次数（操作键与样本旧/新 lot_id） | `_apply_lots/_apply_samples/_sample_delta` |
| `groups_touched` | 调整过的 (supplier, site) 组数 | `_group_delta` |
| `lots_rows_read` | lots 表读取（存在性检查/旧行/样本所属批分组） | `_get_lot` |
| `sample_rows_read` | **读取的关联样本数**（update/delete 取旧行） | `_get_sample` |
| `base_rows_written` | 原表写行数 | 各 `_apply_*` |
| `aux_rows_read` | 辅助表读行数（含批次换组时读样本邻域） | `_sample_delta/_agg_grade_add/_lot_join_group/_grade_count_add/_group_delta` |
| `aux_rows_written` | **调整的辅助键数** | 同上 |
| `elapsed_ms` | 本批耗时 | `apply_batch` |

## 6. 参考查询与核对关系

`mes_pipeline/qcsum/reference.py` 是独立的完整 `LEFT JOIN/GROUP BY` 参考查询，
**只在测试、`qcsum verify`、`qcsum demo`、`qcsum_demo/verify.py` 中运行**，
生产增量路径（`apply_batch`）从不调用它；`qcsum summary` 直接 SELECT 已维护好
的 `qc_summary`，不临时重算。

## 7. 可重复命令

```bash
# 安装（本环境无 pip：venv + .pth 的 editable 安装；有 pip 时 pip install . 亦可）
cd /workspace/mes_pipeline
python3 -m venv --without-pip .venv
echo /workspace/mes_pipeline > .venv/lib/python3.11/site-packages/mes_pipeline.pth

PY=/workspace/mes_pipeline/.venv/bin/python
cd /tmp                                            # 从源码目录外调用

# 生成演示材料（八组场景 25 批）并逐步演示（每步打印汇总+统计+参考核对）
$PY -I -m qcsum_demo.generate --root /workspace/mes_pipeline/qcsum_demo/materials --scenario demo
$PY -I -m mes_pipeline.qcsum demo --work /tmp/qcsum_demo

# 独立核对（不调用生产查询函数，独立开库逐步核对 25/25）
$PY -I -m qcsum_demo.verify --materials /workspace/mes_pipeline/qcsum_demo/materials --work /tmp/qcsum_verify

# 日常使用四步：初始化 → 装载 → 应用批次 → 读汇总/原表/成功账
$PY -I -m mes_pipeline.qcsum init /tmp/q.db
$PY -I -m qcsum_demo.generate --root /tmp/qmat --scenario bench --lots 1000 --samples 5000
$PY -I -m mes_pipeline.qcsum load /tmp/q.db --lots /tmp/qmat/lots.ndjson --samples /tmp/qmat/samples.ndjson
$PY -I -m mes_pipeline.qcsum apply /tmp/q.db /workspace/mes_pipeline/qcsum_demo/materials/steps/S5_02.json
$PY -I -m mes_pipeline.qcsum summary /tmp/q.db
$PY -I -m mes_pipeline.qcsum verify /tmp/q.db      # 独立参考查询核对（非生产路径）

# 故障注入演练（退出码 137 = 进程被真实杀死）
$PY -I -m mes_pipeline.qcsum apply /tmp/q.db B.json --crash-at after_base
$PY -I -m mes_pipeline.qcsum apply /tmp/q.db B.json --crash-at after_commit

# 测试（35 个：校验/八场景逐步/回滚/重送/冲突/两个真实终止点/装载）
cd /workspace/mes_pipeline && .venv/bin/python -I -m unittest discover -s tests -p "test_qcsum_*.py"

# 规模基准：十万批次、百万样本
$PY -I -m qcsum_demo.bench --work /tmp/qcsum_bench --lots 100000 --samples 1000000
```

Python API：`SummaryStore(db)` → `load_initial(lots, samples)` /
`apply_batch(batch_or_json_text)` / `summary()` / `base_rows(table)` / `batches()`。

## 8. 规模实测（本机，seed=42）

装载 100,000 批次 + 1,000,000 样本（含约 1% 孤立样本），随后两次小更改：

| 阶段 | 耗时 | 处理范围（来自 stats） | 峰值内存 |
| --- | --- | --- | --- |
| load_initial（唯一全量扫描） | 42.8 s | 1,100,000 行 | tracemalloc 峰值 2.2 MiB（分块流式） |
| 批次 A：小样本批换组 + 同批删/加样本（3 操作） | **0.93 ms** | 1 批次、2 组；读 lots 3 行、样本 2 行；辅助读 16 / 写 15 | 410 KiB |
| 批次 B：3 条样本跨组转移（3 操作） | **0.68 ms** | 2 批次、2 组；读 lots 6 行、样本 3 行；辅助读 20 / 写 20 | 415 KiB |
| 参考核对（仅核对用，全量连接百万行） | 每次约 2.3–2.8 s | 全表 | — |

两次小更改各只触及几十行（stats 可复查），没有扫描其余百万行；每批之后持久
汇总都与完整 LEFT JOIN/GROUP BY 参考查询逐组一致（600 → 601 组，含空组情形）。
进程峰值 RSS 29 MiB。

## 9. 八组场景逐步演算（五值 = joined/samples/lots/grades/rej_sum）

| 场景 | 逐步结果 |
| --- | --- |
| S1 无样本批次加样本 | (1,0,1,0,NULL) → (1,1,1,1,3) → (2,2,1,2,3) |
| S2 同 grade 逐条撤销 | (3,3,1,2,3) → (2,2,1,2,2) → (1,1,1,1,NULL) → (1,0,1,0,NULL) |
| S3 null 与明确 0 | (1,1,1,1,NULL) → (2,2,1,1,**0**) → (1,1,1,1,NULL) → (3,3,1,2,5) |
| S4 样本跨批次转移 | 甲(1,1,1,1,5)/乙(1,0,1,0,NULL) → 甲(1,0,1,0,NULL)/乙(1,1,1,1,5) |
| S5 批次换组且同批改样本 | 甲(2,2,1,2,3) → 甲组消失，乙(1,1,1,1,7)（只计最终样本 E3） |
| S6 孤立样本/批次删除重建 | 空 → 丁(1,1,1,1,4) → 空 → 丁(1,1,1,1,4) |
| S7 最后批次离组 | (2,1,2,1,1) → (1,0,1,0,NULL) → 整组消失 |
| S8 同键先删后插 | (1,1,1,1,9) → 不变 → 不变 |
