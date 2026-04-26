"""§0cc P2 + P3 回归：_mining_jobs 必须有容量上限 + cli _cleanup_components
必须关 pipeline。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_mining_jobs_dict_has_capacity_cap() -> None:
    """P2 §0cc 回归：_mining_jobs 是裸 dict 无容量/TTL → 长期 API 进程内存
    线性增长，每个完成结果常驻整份 result.to_dict()。必须有容量上限 + FIFO 淘汰。
    """
    from src.api.research_routes import routes

    # 强制清空（防受其他测试污染）
    routes._mining_jobs.clear()

    # 探测：插入 max+10 条目，最旧的应被淘汰
    cap = getattr(routes, "_MINING_JOBS_MAX", None)
    assert cap is not None, (
        "routes 必须暴露 _MINING_JOBS_MAX 容量上限常量；当前为 None（无 cap）"
    )
    assert cap > 0, f"容量必须 > 0；got {cap}"

    # 模拟完成结果常驻
    set_fn = getattr(routes, "_set_mining_job", None)
    assert callable(set_fn), (
        "必须有 _set_mining_job(run_id, payload) helper 强制走 cap 逻辑，"
        "禁止裸 dict 写入绕过淘汰"
    )

    for i in range(cap + 5):
        set_fn(f"run-{i}", {"status": "completed", "data": "x" * 100})

    assert len(routes._mining_jobs) <= cap, (
        f"_mining_jobs 不能超过容量上限 {cap}；got {len(routes._mining_jobs)}"
    )
    # 最早的 5 条应被淘汰
    assert "run-0" not in routes._mining_jobs
    # 最新的应保留
    assert f"run-{cap + 4}" in routes._mining_jobs


def test_backtesting_cli_cleanup_components_closes_pipeline() -> None:
    """P3 §0cc 回归：cli _cleanup_components 只关 writer 不关 pipeline → 在
    复用解释器/测试进程/嵌入式调用场景下，isolated pipeline 的线程池/缓存
    持续遗留。API 版本 cleanup 已同时关 writer + pipeline，CLI 必须对齐。
    """
    from src.backtesting import cli

    fake_writer = MagicMock()
    fake_writer.close = MagicMock()
    fake_pipeline = MagicMock()
    fake_pipeline.shutdown = MagicMock()

    cli._cleanup_components({"writer": fake_writer, "pipeline": fake_pipeline})

    assert fake_writer.close.called, "writer 必须关"
    assert fake_pipeline.shutdown.called, (
        "pipeline 必须关——否则 isolated 线程池/缓存遗留"
    )
