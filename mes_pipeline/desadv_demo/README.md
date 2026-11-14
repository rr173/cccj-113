# EDIFACT DESADV 发货通知接入器（演示与复核）

把供应商 DESADV 通知的**原始字节**抽取为可查询的包装层级与发货明细关系表。
只做抽取、校验与落库；不做库存增减、采购订单管理或物料谱系计算，也不依赖
任何字段依赖发现来猜规则。纯标准库实现，可在 `python -I` 隔离模式下运行。

## 1. 生成材料

```bash
.venv/bin/python -I -m desadv_demo.generate --root desadv_demo/materials
```

| 文件 | 内容 |
| --- | --- |
| `01_multi.edi` | 两个交换：交换1 两条通知、**父包装后到**、**同零件跨箱**、**中文转义备注**（默认 UNA、点小数）；交换2 **自定义 UNA、逗号小数** |
| `02_comma_retransmit.edi` | 与交换2 同身份同字节，用于演示完整重传 |
| `bad_count_mismatch.edi` | 前交换成功；后交换 UNZ 消息数与实际不符 |
| `bad_unt_mismatch.edi` | 前交换成功；后交换 UNT 段数与实际不符 |
| `bad_cycle.edi` | 前交换成功；后交换包装成环 |
| `bad_orphan.edi` | 前交换成功；后交换父包装引用缺失（孤儿） |
| `bad_truncated.edi` | 前交换成功；后交换尾部未闭合 |
| `conflict_same_id.edi` | 与交换1 同发送方/接收方/交换号但载荷不同 |
| `recover_two_exchanges.edi` | 第一个交换成功、第二个失败的恢复演示 |
| `big/100k.edi` | 单交换 **100000 条发货行** 的大文件 |

## 2. 导入与查询（真实命令）

数据库即一个 SQLite 文件；以下用独立 CLI（也可用主 CLI 的 `desadv` 转发，
数据库固定为 `<store>/desadv.sqlite`）。

```bash
DB=/tmp/demo.sqlite
EDI=desadv_demo/materials/01_multi.edi

# 导入，块大小可调（验收用 1/2/7，常用 65536）
.venv/bin/python -I -m mes_pipeline.desadv.cli --db $DB \
    desadv-import --file $EDI --block-size 7

# 查交换与通知
.venv/bin/python -I -m mes_pipeline.desadv.cli --db $DB desadv-list

# 查一条发货行：根→包装完整路径、数量、中文转义备注、来源偏移
.venv/bin/python -I -m mes_pipeline.desadv.cli --db $DB \
    desadv-line --message-no MSG-1 --line-no L01

# 任意包装的子树汇总（按零件号+单位累加，不乘包装件数、不重复算祖先）
.venv/bin/python -I -m mes_pipeline.desadv.cli --db $DB \
    desadv-subtree --message-no MSG-1 --package P1

# 用库存字节偏移从原文件切回未经改写的原始段
.venv/bin/python -I -m mes_pipeline.desadv.cli --db $DB \
    desadv-slice --file $EDI --start 261 --end 341
```

主 CLI 等价写法：

```bash
.venv/bin/python -I -m mes_pipeline.cli --store /tmp/demo_store \
    desadv -- desadv-import --file $EDI --block-size 7
```

## 3. 手工核算依据（MSG-1，托盘 P1 后到）

包装关系：`B1 -> P1`、`B2 -> P1`、`P1` 为根（P1 在文件中后出现）。

| 行 | 包装 | 零件 | 数量 | 单位 |
| --- | --- | --- | --- | --- |
| L01 | B1 | PART-阿尔法 | 3.5 | PCE |
| L02 | B1 | PART-A | 2.5 | KGM |
| L03 | B2 | PART-A | 10 | PCE |

`P1` 子树汇总（B1+B2，PCE/KGM **分别**汇总、互不相加、不乘 PAC 件数）：

- PART-阿尔法 = 3.5 PCE
- PART-A = 2.5 KGM
- PART-A = 10 PCE

中文备注 `中文?备注+尾` 在原文里写作含释放符的 `中文??备注?+尾`，
查询返回解码后文本；用字节偏移切回的仍是含 `?` 的原始段。

## 4. 块大小不变性验收

同一份材料分别以 1、2、7 字节和常用块大小读入，业务表与来源偏移必须完全相同：

```bash
for BS in 1 2 7 4096; do
  .venv/bin/python -I -m mes_pipeline.desadv.cli --db /tmp/bs_$BS.sqlite \
      desadv-import --file $EDI --block-size $BS
done
```

自动化独立复核（不调用生产查询函数，独立重算子树并逐项核对）：

```bash
.venv/bin/python -I -m desadv_demo.verify \
    --materials desadv_demo/materials --work /tmp/dv_work
```

## 5. 故障与恢复演示

```bash
# 各类坏交换：退出码 4（REJECTED），报错带段标签与字节位置；前一交换仍可查
for f in bad_count_mismatch bad_unt_mismatch bad_cycle bad_orphan \
         bad_truncated recover_two_exchanges; do
  .venv/bin/python -I -m mes_pipeline.desadv.cli --db /tmp/$f.sqlite \
      desadv-import --file desadv_demo/materials/$f.edi --block-size 3
done

# 同号改文冲突：退出码 5（CONFLICT），旧资料保持不动
.venv/bin/python -I -m mes_pipeline.desadv.cli --db $DB \
    desadv-import --file desadv_demo/materials/conflict_same_id.edi

# 完整字节重传：outcome=duplicate，不新增业务行、不翻倍
.venv/bin/python -I -m mes_pipeline.desadv.cli --db $DB \
    desadv-import --file $EDI --block-size 4096
```

### 提交前退出的可重复故障注入

`--fault-inject-before-commit` 在首个交换全部校验通过、COMMIT 前以退出码 11
杀掉进程。重启后查库应无该交换的任何半条记录；再次正常导入可完整成功：

```bash
rm -f /tmp/fault.sqlite
.venv/bin/python -I -m mes_pipeline.desadv.cli --db /tmp/fault.sqlite \
    desadv-import --file $EDI --fault-inject-before-commit; echo "exit=$?"  # 11
.venv/bin/python -I -m mes_pipeline.desadv.cli --db /tmp/fault.sqlite desadv-list
.venv/bin/python -I -m mes_pipeline.desadv.cli --db /tmp/fault.sqlite \
    desadv-import --file $EDI --block-size 7
```

## 6. 十万行大文件与峰值内存

```bash
.venv/bin/python -I - <<'PY'
import resource, time
from mes_pipeline.desadv.service import import_file
t=time.perf_counter()
r=import_file("/tmp/big.sqlite", "desadv_demo/materials/big/100k.edi",
               block_size=65536)
print(f"行数={r.total_lines} 耗时={time.perf_counter()-t:.1f}s "
      f"峰值RSS={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.1f}MiB")
PY
```

实测（本机参考值）：100000 行、220010 段，约 4.3 秒、峰值约 52 MiB
（主要为 SQLite 页缓存）；文件总大小不受内存限制，单段长度可用
`--max-segment-bytes` 声明上限并拒绝超限。

## 7. 关系表与来源追溯

- `desadv_exchange` 交换（UNB/UNZ 身份、UNA、小数符、载荷 SHA-256、字节跨度）
- `desadv_message` 通知（UNH/BGM/DTM/两个 NAD）
- `desadv_segment` 全部原始段（含原始字节 BLOB、交换/消息段序号、起止字节）
- `desadv_package` 包装层级（父子、深度、件数、类型、标签号）
- `desadv_line` 发货行（精确十进制文本 + 分子/分母、单位、LIN/QTY 偏移）
- `desadv_ftx` 发货行备注（保留次序与原文、FTX 偏移）

每条业务行都可经 `desadv_segment` 追溯到来源文件、交换、消息、段序号与
原文件字节偏移（左闭右开、含段结束符）；偏移是**字节**偏移，不是解码后的
字符位置，可直接切回未经改写的原始段。
