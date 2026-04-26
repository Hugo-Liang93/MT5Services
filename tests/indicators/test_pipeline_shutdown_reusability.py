"""§0aa P1 回归：OptimizedPipeline.shutdown() 必须清全局 _global_executor，
否则下一次 mode 切换回 full/observe 时拿到的还是同一个已 shutdown 的 executor，
RuntimeError: cannot schedule new futures after shutdown 把指标主链永久毒化。
"""
from __future__ import annotations

import pytest

from src.indicators.engine.pipeline import OptimizedPipeline, PipelineConfig
from src.indicators.engine import parallel_executor as pe_module


def test_pipeline_shutdown_clears_global_executor_to_allow_reuse() -> None:
    """P1 §0aa 回归：旧 shutdown 仅清 self._executor 不清模块全局
    _global_executor → 同进程 lifecycle 内 mode 切到 ingest_only/risk_off
    再切回时拿到同一已 shutdown 的全局 executor，submit 抛 RuntimeError。
    """
    # 防御：测试开始前先清全局，避免被前序测试污染
    pe_module.shutdown_global_executor(wait=True)

    config = PipelineConfig(enable_parallel=True, max_workers=2)
    pipeline_a = OptimizedPipeline(config=config)

    # 触发 _global_executor 创建
    executor_a = pipeline_a.executor
    assert executor_a is not None

    # 模拟 mode 切到 ingest_only/risk_off：调用 shutdown()
    pipeline_a.shutdown()

    # 模拟 mode 切回 full：新建 pipeline 再访问 executor
    pipeline_b = OptimizedPipeline(config=config)
    executor_b = pipeline_b.executor

    # 旧 bug：executor_b 仍是已 shutdown 的全局 → submit 立刻抛异常
    # 新行为：shutdown 清掉全局，executor_b 是新建的 fresh executor
    try:
        future = executor_b.executor.submit(lambda: 42)
        result = future.result(timeout=2)
        assert result == 42, f"executor 必须能正常 submit；got {result!r}"
    except RuntimeError as exc:
        if "cannot schedule new futures after shutdown" in str(exc):
            pytest.fail(
                f"§0aa P1: pipeline.shutdown() 没清全局 _global_executor，"
                f"下次切回时全局 executor 仍是 shutdown 状态，submit 失败：{exc}"
            )
        raise
    finally:
        pipeline_b.shutdown()
        pe_module.shutdown_global_executor(wait=True)
