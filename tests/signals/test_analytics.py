from __future__ import annotations

from datetime import datetime, timezone

from src.signals.analytics import (
    AnalyticsPluginRegistry,
    DiagnosticThresholds,
    SignalDiagnosticsAnalyzer,
)


def test_signal_diagnostics_analyzer_builds_expected_report() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [
        {
            "generated_at": "2026-03-19T10:00:00+00:00",
            "strategy": "sma_trend",
            "direction": "buy",
            "confidence": 0.9,
            "reason": "trend",
            "metadata": {"bar_time": "2026-03-19T10:00:00+00:00", "regime": "trending"},
        },
        {
            "generated_at": "2026-03-19T10:00:00+00:00",
            "strategy": "rsi_reversion",
            "direction": "sell",
            "confidence": 0.7,
            "reason": "rsi",
            "metadata": {"bar_time": "2026-03-19T10:00:00+00:00", "regime": "trending"},
        },
        {
            "generated_at": "2026-03-19T10:05:00+00:00",
            "strategy": "rsi_reversion",
            "direction": "hold",
            "confidence": 0.1,
            "reason": "missing_required_indicator:rsi",
            "metadata": {"bar_time": "2026-03-19T10:05:00+00:00", "regime": "ranging"},
        },
    ]
    report = analyzer.build_report(
        rows,
        symbol="XAUUSD",
        timeframe="M5",
        scope="confirmed",
        thresholds=DiagnosticThresholds(),
    )

    assert report["rows_analyzed"] == 3
    assert report["conflict"]["bars_with_buy_sell_conflict"] == 1
    assert report["dominant_regime"] == "trending"
    assert report["session_distribution"]["london"] == 3
    assert report["strategy_session_breakdown"]["rsi_reversion"]["london"] == 2
    assert any(item["strategy"] == "rsi_reversion" for item in report["strategy_health"])
    assert len(report["recommendations"]) >= 1


def test_signal_diagnostics_analyzer_daily_quality_report_filters_24h_window() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [
        {
            "generated_at": "2026-03-19T10:00:00+00:00",
            "strategy": "sma_trend",
            "direction": "buy",
            "confidence": 0.8,
            "reason": "trend",
            "metadata": {"bar_time": "2026-03-19T10:00:00+00:00", "regime": "trending"},
        },
        {
            "generated_at": "2026-03-17T10:00:00+00:00",
            "strategy": "rsi_reversion",
            "direction": "hold",
            "confidence": 0.1,
            "reason": "missing_required_indicator:rsi",
            "metadata": {"bar_time": "2026-03-17T10:00:00+00:00", "regime": "ranging"},
        },
    ]
    report = analyzer.build_daily_quality_report(
        rows,
        symbol="XAUUSD",
        timeframe="M5",
        scope="confirmed",
        thresholds=DiagnosticThresholds(),
        now=datetime(2026, 3, 19, 12, 0, tzinfo=timezone.utc),
    )

    assert report["rows_analyzed"] == 1
    assert report["window"]["hours"] == 24
    assert report["session_distribution"]["london"] == 1
    assert report["rows_before_window_filter"] == 2
    assert report["skipped_rows_invalid_time"] == 0


def test_signal_diagnostics_analyzer_supports_extensions_plugin() -> None:
    plugins = AnalyticsPluginRegistry()
    plugins.register(
        "action_entropy",
        lambda rows, _report: {"unique_actions": len({str(r.get("direction", "")) for r in rows})},
    )
    analyzer = SignalDiagnosticsAnalyzer(plugin_registry=plugins)
    rows = [
        {"generated_at": "2026-03-19T10:00:00+00:00", "strategy": "a", "direction": "buy", "confidence": 0.9},
        {"generated_at": "2026-03-19T10:01:00+00:00", "strategy": "b", "direction": "sell", "confidence": 0.8},
    ]

    report = analyzer.build_report(
        rows,
        symbol="XAUUSD",
        timeframe="M5",
        scope="confirmed",
        thresholds=DiagnosticThresholds(),
    )

    assert report["extensions"]["action_entropy"]["unique_actions"] == 2


def test_daily_quality_report_counts_invalid_time_rows() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [
        {"generated_at": "bad_time", "strategy": "a", "direction": "buy", "confidence": 0.9},
        {"generated_at": "2026-03-19T10:00:00+00:00", "strategy": "b", "direction": "sell", "confidence": 0.8},
    ]
    report = analyzer.build_daily_quality_report(
        rows,
        symbol="XAUUSD",
        timeframe="M5",
        scope="confirmed",
        thresholds=DiagnosticThresholds(),
        now=datetime(2026, 3, 19, 12, 0, tzinfo=timezone.utc),
    )

    assert report["rows_before_window_filter"] == 2
    assert report["skipped_rows_invalid_time"] == 1
