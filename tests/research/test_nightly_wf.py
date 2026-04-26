"""Nightly WF 契约 + 回归检测测试。

聚焦在契约校验和比较逻辑（纯函数部分）。runner.py 涉及完整回测执行需集成环境，
不在单测范围。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.research.nightly import (
    NightlyWFConfig,
    NightlyWFReport,
    RegressionFinding,
    StrategyMetrics,
    compare_reports,
)


def _cfg(*, sharpe_t: float = 0.20, dd_t: float = 0.15) -> NightlyWFConfig:
    return NightlyWFConfig(
        symbol="XAUUSD",
        timeframes=("M15",),
        start_date="2023-01-01",
        end_date="2026-03-30",
        strategies=("trend_continuation",),
        sharpe_regression_threshold=sharpe_t,
        dd_regression_threshold=dd_t,
        workers=1,
        output_dir="/tmp",
    )


def _metric(**kwargs) -> StrategyMetrics:
    defaults = dict(
        strategy="trend_continuation", timeframe="M15", trades=100,
        win_rate=0.55, pnl=1000.0, profit_factor=1.8,
        sharpe=1.5, sortino=2.0, max_dd=0.10,
        expectancy=10.0, avg_bars_held=20.0,
    )
    defaults.update(kwargs)
    return StrategyMetrics(**defaults)


def _report(metrics, *, cfg=None) -> NightlyWFReport:
    return NightlyWFReport(
        generated_at=datetime.now(timezone.utc),
        config=cfg or _cfg(),
        metrics=tuple(metrics),
    )


class TestConfigContract:
    def test_empty_timeframes_raises(self) -> None:
        with pytest.raises(ValueError):
            NightlyWFConfig(
                symbol="XAUUSD", timeframes=(), start_date="x", end_date="x",
                strategies=(), sharpe_regression_threshold=0.1, dd_regression_threshold=0.1,
                workers=1, output_dir="/tmp",
            )

    def test_zero_workers_raises(self) -> None:
        with pytest.raises(ValueError):
            _cfg_kwargs = dict(
                symbol="XAUUSD", timeframes=("M15",), start_date="x", end_date="x",
                strategies=(), sharpe_regression_threshold=0.1, dd_regression_threshold=0.1,
                output_dir="/tmp",
            )
            NightlyWFConfig(workers=0, **_cfg_kwargs)

    def test_zero_thresholds_raise(self) -> None:
        with pytest.raises(ValueError):
            _cfg(sharpe_t=0.0)
        with pytest.raises(ValueError):
            _cfg(dd_t=0.0)


class TestRegressionDetection:
    def test_sharpe_regression(self) -> None:
        prev = _report([_metric(sharpe=1.5)])
        cur = _report([_metric(sharpe=0.9)])
        findings = compare_reports(current=cur, previous=prev, config=_cfg())
        sharpe = next(f for f in findings if f.metric == "sharpe")
        assert sharpe.severity == "regression"
        # (0.9 - 1.5) / 1.5 = -40%
        assert sharpe.change_ratio == pytest.approx(-0.4)

    def test_sharpe_improvement(self) -> None:
        prev = _report([_metric(sharpe=1.0)])
        cur = _report([_metric(sharpe=1.5)])
        findings = compare_reports(current=cur, previous=prev, config=_cfg())
        sharpe = next(f for f in findings if f.metric == "sharpe")
        assert sharpe.severity == "improvement"

    def test_sharpe_unchanged(self) -> None:
        prev = _report([_metric(sharpe=1.0)])
        cur = _report([_metric(sharpe=1.05)])
        findings = compare_reports(current=cur, previous=prev, config=_cfg())
        sharpe = next(f for f in findings if f.metric == "sharpe")
        assert sharpe.severity == "unchanged"

    def test_max_dd_regression(self) -> None:
        # DD from 10% to 18% = +80% relative → 恶化 (regression)
        prev = _report([_metric(max_dd=0.10)])
        cur = _report([_metric(max_dd=0.18)])
        findings = compare_reports(current=cur, previous=prev, config=_cfg())
        dd = next(f for f in findings if f.metric == "max_dd")
        assert dd.severity == "regression"

    def test_max_dd_improvement(self) -> None:
        # DD from 15% to 5% = -66% relative → 改善
        prev = _report([_metric(max_dd=0.15)])
        cur = _report([_metric(max_dd=0.05)])
        findings = compare_reports(current=cur, previous=prev, config=_cfg())
        dd = next(f for f in findings if f.metric == "max_dd")
        assert dd.severity == "improvement"

    def test_new_strategy_no_comparison(self) -> None:
        # 前次不含该策略 → 无对比行
        prev = _report([])
        cur = _report([_metric()])
        findings = compare_reports(current=cur, previous=prev, config=_cfg())
        assert findings == ()

    def test_has_regression_flag(self) -> None:
        prev = _report([_metric(sharpe=1.5)])
        cur_metrics = [_metric(sharpe=0.5)]
        cur = _report(cur_metrics)
        findings = compare_reports(current=cur, previous=prev, config=_cfg())
        new_cur = NightlyWFReport(
            generated_at=datetime.now(timezone.utc),
            config=_cfg(),
            metrics=tuple(cur_metrics),
            regressions=findings,
        )
        assert new_cur.has_regression()


class TestReportSerialization:
    def test_metric_to_dict(self) -> None:
        d = _metric().to_dict()
        assert d["strategy"] == "trend_continuation"
        assert d["sharpe"] == 1.5
        assert "pnl" in d

    def test_report_to_dict(self) -> None:
        r = _report([_metric()])
        d = r.to_dict()
        assert "metrics" in d
        assert d["has_regression"] is False
        assert len(d["metrics"]) == 1


# ── §0cc P2 回归：nightly_wf 异常隔离 + cleanup ──


def test_run_single_combo_packed_propagates_infrastructure_errors() -> None:
    """P2 §0cc 回归：旧 _run_single_combo_packed 把所有 Exception catch 成
    字符串返回 → ConnectionError / 数据库不可用 / 数据加载 bug 被汇总进
    failed_runs，主流程返回看似正常的 report，违反"基础设施错误应直接抛出"契约。
    """
    import builtins
    from src.research.nightly import runner as nightly_runner

    original = nightly_runner._run_single_combo

    def boom(**kwargs):
        raise ConnectionError("db down")

    nightly_runner._run_single_combo = boom
    try:
        with pytest.raises(ConnectionError):
            nightly_runner._run_single_combo_packed(
                ("XAUUSD", "trendline", "M15", "2026-01-01T00:00:00", "2026-01-02T00:00:00")
            )
    finally:
        nightly_runner._run_single_combo = original


def test_run_single_combo_packed_returns_string_for_business_errors() -> None:
    """对称契约：业务类异常（StrategyError 等非基础设施类）仍可 catch 成字符串
    便于聚合（保留旧"业务失败不阻断 nightly"语义）。
    """
    from src.research.nightly import runner as nightly_runner

    original = nightly_runner._run_single_combo

    class StrategyError(Exception):
        pass

    def biz_fail(**kwargs):
        raise StrategyError("strategy returned no signals")

    nightly_runner._run_single_combo = biz_fail
    try:
        result = nightly_runner._run_single_combo_packed(
            ("XAUUSD", "trendline", "M15", "2026-01-01T00:00:00", "2026-01-02T00:00:00")
        )
        assert isinstance(result, str), f"业务异常应 catch 成字符串；got {type(result)}"
        assert "StrategyError" in result
    finally:
        nightly_runner._run_single_combo = original


def test_run_single_combo_calls_cleanup_components_in_finally(monkeypatch) -> None:
    """P2 §0cc 回归：_run_single_combo 每次都 build_backtest_components 但
    没 finally cleanup → writer / pipeline / connection pool / thread pool 持续
    累积，长 nightly 作业会堆死。必须在 finally 关闭。
    """
    from src.research.nightly import runner as nightly_runner

    fake_writer = MagicMock()
    fake_writer.close = MagicMock()
    fake_pipeline = MagicMock()
    fake_pipeline.shutdown = MagicMock()
    fake_data_loader = MagicMock()
    fake_signal_module = MagicMock()
    fake_regime_detector = MagicMock()

    def fake_build_components(**kwargs):
        return {
            "data_loader": fake_data_loader,
            "signal_module": fake_signal_module,
            "pipeline": fake_pipeline,
            "regime_detector": fake_regime_detector,
            "writer": fake_writer,
            "performance_tracker": None,
        }

    fake_engine = MagicMock()
    fake_result = MagicMock()
    fake_result.metrics = SimpleNamespace(
        total_trades=0, win_rate=0.0, total_pnl=0.0, profit_factor=0.0,
        sharpe_ratio=0.0, sortino_ratio=0.0, max_drawdown=0.0, expectancy=0.0,
        avg_bars_held=0.0,
    )
    fake_engine.run.return_value = fake_result

    monkeypatch.setattr(
        "src.backtesting.component_factory.build_backtest_components",
        fake_build_components,
    )
    monkeypatch.setattr(
        "src.backtesting.engine.BacktestEngine",
        lambda **kwargs: fake_engine,
    )

    nightly_runner._run_single_combo(
        symbol="XAUUSD",
        strategy="trendline",
        tf="M15",
        start="2026-01-01T00:00:00",
        end="2026-01-02T00:00:00",
    )

    assert fake_writer.close.called, (
        "writer.close() 必须在 finally 里调用，否则 nightly 长跑会堆死连接池"
    )
    assert fake_pipeline.shutdown.called, (
        "pipeline.shutdown() 必须在 finally 里调用，否则线程池/缓存累积"
    )


def test_run_single_combo_cleanup_runs_even_when_engine_raises(monkeypatch) -> None:
    """对称契约：engine.run() 抛异常时 cleanup 仍必须跑。"""
    from src.research.nightly import runner as nightly_runner

    fake_writer = MagicMock()
    fake_pipeline = MagicMock()

    def fake_build_components(**kwargs):
        return {
            "data_loader": MagicMock(),
            "signal_module": MagicMock(),
            "pipeline": fake_pipeline,
            "regime_detector": MagicMock(),
            "writer": fake_writer,
            "performance_tracker": None,
        }

    fake_engine = MagicMock()
    fake_engine.run.side_effect = RuntimeError("engine boom")

    monkeypatch.setattr(
        "src.backtesting.component_factory.build_backtest_components",
        fake_build_components,
    )
    monkeypatch.setattr(
        "src.backtesting.engine.BacktestEngine",
        lambda **kwargs: fake_engine,
    )

    with pytest.raises(RuntimeError, match="engine boom"):
        nightly_runner._run_single_combo(
            symbol="XAUUSD", strategy="trendline", tf="M15",
            start="2026-01-01T00:00:00", end="2026-01-02T00:00:00",
        )
    assert fake_writer.close.called, "engine 抛异常时 cleanup 仍必须跑"
    assert fake_pipeline.shutdown.called
