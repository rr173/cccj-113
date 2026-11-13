"""规模样本基准测试（默认跳过；设 DEPS_BENCH=1 时运行）。

验证「不小于 12 列、两万行、k=3」的样本能实际跑完发现，并记录耗时与最高内存；
同时确认覆盖的组合数等于 1..k 的全部组合数（完整，不是抽样）。
"""
from __future__ import annotations

import os
import time

import pytest

from synthetic import deps_generate
from mes_pipeline.deps.engine import build_index, discover
from mes_pipeline.deps.io import load_dataset

pytestmark = pytest.mark.skipif(
    os.environ.get("DEPS_BENCH") != "1",
    reason="规模基准默认跳过；设 DEPS_BENCH=1 运行")


def test_twelve_columns_twenty_thousand_rows_k3_completes(tmp_path, capsys=None):
    root = tmp_path / "bench"
    deps_generate.write_bench(root, rows=20_000)
    dataset = load_dataset(root / "columns.json", root / "rows.jsonl")
    assert dataset.column_count == 12
    assert dataset.row_count == 20_000

    started = time.perf_counter()
    index = build_index(dataset)
    result = discover(index, 3, 2, "equal")
    elapsed = time.perf_counter() - started

    expected = sum(
        __import__("math").comb(12, size) for size in range(1, 4))  # 12+66+220
    assert expected == 298
    assert result.stats["subsets_in_scope"] == expected
    assert result.stats["subsets_enumerated"] == expected
    assert result.complete is True
    assert result.claimable is True
    assert result.keys and result.fds
    print(f"12x20000 k=3 equal: {elapsed:.2f}s, "
          f"{len(result.keys)} keys, {len(result.fds)} fds")
    assert elapsed < 300

    started = time.perf_counter()
    result_ignore = discover(index, 3, 2, "ignore")
    elapsed_ignore = time.perf_counter() - started
    assert result_ignore.stats["subsets_enumerated"] == expected
    print(f"12x20000 k=3 ignore: {elapsed_ignore:.2f}s")
    assert elapsed_ignore < 300
