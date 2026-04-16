"""Nightly WF 契约 + 回归检测测试。

聚焦在契约校验和比较逻辑（纯函数部分）。runner.py 涉及完整回测执行需集成环境，
不在单测范围。
"""

from __future__ import annotations

from datetime import datetime, timezone

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
