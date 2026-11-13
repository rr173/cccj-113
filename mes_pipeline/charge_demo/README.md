# 配料计算演示材料

八组自制小数据，覆盖：散装已知最优解、整桶导致连续松弛不可落地、检出限区间
使中点法误判、原料互斥、库存不足、ppm 与百分数混用、非法输入拒绝与修正后重解。

所有命令在仓库根目录执行，解释器为 `.venv/bin/python`（项目已 editable 安装，
`-I` 隔离模式可正常运行）。

## 0. 输入文件格式

| 文件 | 说明 |
| --- | --- |
| `inventory.csv` | `lot_id,material,mode,available_kg,price_per_kg,drum_net_kg`；`mode` 为 `bulk`（连续取用）或 `drum`（整桶，须给 `drum_net_kg`） |
| `assays.csv` | `lot_id,element,cert_id,unit,value_type,value,lower,upper`；`unit` ∈ `pct/ppm`；`value_type` ∈ `single/range/limit`（`limit` 即 `<x`，闭区间 [0,x]） |
| `certificates.csv` | `cert_id,lab,issued_at`，化验行引用的证书必须登记在册 |
| `heat.json` | `heat_id`、`total_mass_kg`、`elements`（`min_pct/max_pct/min_ppm/max_ppm`）、`mutex`（互斥 lot 对） |

## 1. 求解与查看

```bash
S=/tmp/charge_store
# 求解（--calc-id 可省略，缺省为 <heat_id>-<时间戳>）
.venv/bin/python -I -m mes_pipeline.cli --store $S charge-solve \
    --inventory charge_demo/s1_bulk_optimum/inventory.csv \
    --assays    charge_demo/s1_bulk_optimum/assays.csv \
    --certs     charge_demo/s1_bulk_optimum/certificates.csv \
    --heat      charge_demo/s1_bulk_optimum/heat.json \
    --calc-id demo-s1_bulk_optimum
# 查看某次计算结果（成功/失败均可查看）
.venv/bin/python -I -m mes_pipeline.cli --store $S charge-show --calc-id demo-s1_bulk_optimum
```

退出码：`0` 最优 / `3` 约束无解 / `4` 输入不合法 / `5` 求解未收敛。

## 2. 七个场景与预期结论

| 场景 | 命令中的目录 | 预期 |
| --- | --- | --- |
| 散装已知最优解 | `s1_bulk_optimum` | OPTIMAL，费用 5000（LOT-FE-A 500 kg + LOT-FE-B 500 kg，Cr 恰好 18%） |
| 整桶松弛不可落地 | `s2_drum_relaxation` | OPTIMAL 550；连续松弛 500 需 2.5 桶无法落地；整数最优为 3+3 桶 + 5 kg 散装；两批各 15 kg 不足一桶不可用 |
| 检出限中点误判 | `s3_detection_limit` | INFEASIBLE；`<20 ppm` 按 [0,20] ppm 保守核算突破 15 ppm 上限；中点法（10 ppm）会误判可行 |
| 修正后重解 | `s3_fixed` | 复测单值 8 ppm（新证书 CERT-PA-02）后 OPTIMAL，费用 200 |
| 原料互斥 | `s4_mutex` | OPTIMAL 350，仅用 LOT-MX-2；无互斥松弛 325 会选择被禁止的组合 |
| 库存不足 | `s5_shortage` | INFEASIBLE；冲突集 = 质量守恒 + 三批可用上限（可用 450 kg < 需求 500 kg） |
| 单位混用 | `s6_mixed_units` | OPTIMAL 1400；Cr 同时以 pct 与 ppm 化验、Pb 含 `<5 ppm`，统一为质量分数求解 |
| 非法输入 | `s7_invalid_input` | 缺证书 / 结果冲突 / 负数 / 单位不明 / 缺化验 均 INVALID_INPUT；`assays_ok.csv` 修正后 OPTIMAL 250 |

s7 的各非法变体（共用同一 `inventory.csv`/`certificates.csv`/`heat.json`）：

```bash
for v in missing_cert conflict negative unknown_unit missing_element ok; do
  .venv/bin/python -I -m mes_pipeline.cli --store $S charge-solve \
      --inventory charge_demo/s7_invalid_input/inventory.csv \
      --assays    charge_demo/s7_invalid_input/assays_$v.csv \
      --certs     charge_demo/s7_invalid_input/certificates.csv \
      --heat      charge_demo/s7_invalid_input/heat.json \
      --calc-id demo-s7-$v
done
```

## 3. 独立复核

`verify.py` 不复用求解器结论，用普通 Python 重新解析输入并逐项重算：

```bash
# 全量复核（先按上文求解全部场景）
.venv/bin/python -I charge_demo/verify.py verify-all --store $S

# 单项：
.venv/bin/python -I charge_demo/verify.py verify-plan       --store $S --calc-id demo-s1_bulk_optimum
.venv/bin/python -I charge_demo/verify.py verify-exhaustive --scenario charge_demo/s2_drum_relaxation \
    --store $S --calc-id demo-s2_drum_relaxation
.venv/bin/python -I charge_demo/verify.py verify-iis        --store $S --calc-id demo-s5_shortage
.venv/bin/python -I charge_demo/verify.py verify-midpoint   --scenario charge_demo/s3_detection_limit
```

- `verify-plan`：重算逐批费用、总重、最坏成分、未用库存，核对证书有效性；
- `verify-exhaustive`：穷举 s2 全部桶数组合（27 个有效组合），核对最优费用 550；
- `verify-iis`：冲突集整体重新求解不可行，逐条移除后恢复可行；
- `verify-midpoint`：对照演示中点法如何把 s3 误判为可行。

## 4. 计算口径

1. 含量统一为质量分数（1% = 1e-2，1 ppm = 1e-6）后求解，展示时换算回炉次书写单位。
2. 化验值一律为闭区间：单值 `[v,v]`；上下界 `[lo,hi]`；`<x` 为 `[0,x]`，**不取中点**。
3. 保守核算：最低含量约束用各原料区间**下界**、最高含量约束用**上界**，
   保证区间内任何真实成分都不突破炉次要求；不把区间收缩成平均数。
4. 整桶批可用量 = `floor(available_kg / drum_net_kg) × drum_net_kg`，余量不可用。
5. 目标为总投料费用最小；多最优时给出各批次在最优费用下的投料区间。
6. 不可行时给出极小冲突约束集（删除过滤法），整体不可行、移除任意一条即可行。
