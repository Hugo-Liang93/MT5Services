"""§0cc P2 回归：build_research_data_deps 必须暴露 close 方法让 mining
入口能正确清理 writer/pipeline。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_build_research_data_deps_exposes_close(monkeypatch) -> None:
    """P2 §0cc 回归：build_research_data_deps 调 build_backtest_components
    返新的 writer + pipeline，但只把 data_loader/pipeline/regime_detector 包进
    ResearchDataDeps 就返回，没有 close/shutdown/context manager → mining 入口
    根本拿不到合法的释放端口，writer 连接池 + pipeline 线程池静默累积。
    """
    from src.backtesting import component_factory

    fake_writer = MagicMock()
    fake_writer.close = MagicMock()
    fake_pipeline = MagicMock()
    fake_pipeline.shutdown = MagicMock()
    fake_data_loader = MagicMock()
    fake_regime_detector = MagicMock()

    def fake_build(**kwargs):
        return {
            "data_loader": fake_data_loader,
            "signal_module": MagicMock(),
            "pipeline": fake_pipeline,
            "regime_detector": fake_regime_detector,
            "writer": fake_writer,
            "performance_tracker": None,
        }

    monkeypatch.setattr(component_factory, "build_backtest_components", fake_build)

    deps = component_factory.build_research_data_deps()

    # 必须暴露 close()
    close = getattr(deps, "close", None)
    assert callable(close), (
        "ResearchDataDeps 必须暴露 close()/shutdown()/context manager 让 mining 入口清理"
    )

    # 调用 close 必须级联关闭 writer + pipeline
    close()
    assert fake_writer.close.called, (
        "deps.close() 必须级联调 writer.close()，否则连接池泄漏"
    )
    assert fake_pipeline.shutdown.called, (
        "deps.close() 必须级联调 pipeline.shutdown()，否则线程池泄漏"
    )


def test_research_data_deps_supports_context_manager(monkeypatch) -> None:
    """对称契约：ResearchDataDeps 应支持 with 语法自动 cleanup（Pythonic）。"""
    from src.backtesting import component_factory

    fake_writer = MagicMock()
    fake_pipeline = MagicMock()

    def fake_build(**kwargs):
        return {
            "data_loader": MagicMock(),
            "signal_module": MagicMock(),
            "pipeline": fake_pipeline,
            "regime_detector": MagicMock(),
            "writer": fake_writer,
            "performance_tracker": None,
        }

    monkeypatch.setattr(component_factory, "build_backtest_components", fake_build)

    deps = component_factory.build_research_data_deps()
    with deps:
        pass

    assert fake_writer.close.called
    assert fake_pipeline.shutdown.called
