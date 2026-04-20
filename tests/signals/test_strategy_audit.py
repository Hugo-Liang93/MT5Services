"""SignalDiagnosticsAnalyzer.build_strategy_audit_report 单元测试（backlog P0.3）。

验证：
- 按 strategy 分组聚合：signals/actionable/blocked/hold counts
- hold = total - actionable - blocked（旧记录 actionability=None 算 hold）
- conflict_count: 同 strategy 同 bar 出现 buy+sell（罕见但要正确处理）
- avg_confidence / last_signal_at / hold_rate / blocked_rate
- win_rate 从 winrate_rows 按 strategy 加权聚合
- status: warn 当任一阈值越限
- recent_issue 取首个 warning
- category 透传
"""

from __future__ import annotations

import pytest

from src.signals.analytics.diagnostics import (
    DiagnosticThresholds,
    SignalDiagnosticsAnalyzer,
    _aggregate_winrates_by_strategy,
)


def _row(
    strategy: str,
    direction: str = "buy",
    confidence: float = 0.7,
    actionability: str | None = None,
    generated_at: str = "2026-04-20T10:00:00+00:00",
    bar_time: str | None = None,
) -> dict:
    metadata = {}
    if bar_time is not None:
        metadata["bar_time"] = bar_time
    return {
        "strategy": strategy,
        "direction": direction,
        "confidence": confidence,
        "actionability": actionability,
        "generated_at": generated_at,
        "metadata": metadata,
    }


# ── _aggregate_winrates_by_strategy ──────────────────────────────


def test_aggregate_winrates_sums_buys_and_sells_per_strategy() -> None:
    rows = [
        {"strategy": "alpha", "direction": "buy", "total": 10, "wins": 6},
        {"strategy": "alpha", "direction": "sell", "total": 10, "wins": 4},
        {"strategy": "beta", "direction": "buy", "total": 5, "wins": 5},
    ]
    result = _aggregate_winrates_by_strategy(rows)
    assert result == {"alpha": 0.5, "beta": 1.0}


def test_aggregate_winrates_returns_none_for_zero_total() -> None:
    result = _aggregate_winrates_by_strategy(
        [{"strategy": "x", "direction": "buy", "total": 0, "wins": 0}]
    )
    assert result == {"x": None}


def test_aggregate_winrates_skips_blank_strategy() -> None:
    result = _aggregate_winrates_by_strategy(
        [{"strategy": "", "direction": "buy", "total": 5, "wins": 3}]
    )
    assert result == {}


# ── build_strategy_audit_report ──────────────────────────────────


def test_audit_report_groups_by_strategy_and_counts_admission() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [
        _row("alpha", actionability="actionable"),
        _row("alpha", actionability="actionable"),
        _row("alpha", actionability="blocked"),
        _row("alpha", actionability=None),  # hold（旧记录）
        _row("beta", actionability="hold"),
    ]
    report = analyzer.build_strategy_audit_report(rows)

    by_strategy = {s["strategy"]: s for s in report["strategies"]}
    alpha = by_strategy["alpha"]
    assert alpha["signals"] == 4
    assert alpha["actionable_signals"] == 2
    assert alpha["blocked_count"] == 1
    assert alpha["hold_count"] == 1  # actionability=None 算 hold
    assert alpha["hold_rate"] == 0.25
    assert alpha["blocked_rate"] == 0.25

    beta = by_strategy["beta"]
    assert beta["signals"] == 1
    assert beta["actionable_signals"] == 0
    assert beta["blocked_count"] == 0
    assert beta["hold_count"] == 1


def test_audit_report_average_confidence_and_last_signal_at() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [
        _row("alpha", confidence=0.4, generated_at="2026-04-20T09:00:00+00:00"),
        _row("alpha", confidence=0.8, generated_at="2026-04-20T11:00:00+00:00"),
    ]
    report = analyzer.build_strategy_audit_report(rows)
    alpha = report["strategies"][0]
    assert alpha["avg_confidence"] == pytest.approx(0.6)
    assert alpha["last_signal_at"] == "2026-04-20T11:00:00+00:00"


def test_audit_report_conflict_count_per_strategy_buy_sell_same_bar() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [
        _row("alpha", direction="buy", bar_time="2026-04-20T10:00:00+00:00"),
        _row("alpha", direction="sell", bar_time="2026-04-20T10:00:00+00:00"),
        _row("alpha", direction="buy", bar_time="2026-04-20T11:00:00+00:00"),
    ]
    report = analyzer.build_strategy_audit_report(rows)
    alpha = report["strategies"][0]
    assert alpha["conflict_count"] == 1  # 仅 10:00 bar 同时出现 buy+sell


def test_audit_report_attaches_winrate_from_external_rows() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [_row("alpha")]
    winrate_rows = [
        {"strategy": "alpha", "direction": "buy", "total": 10, "wins": 6},
    ]
    report = analyzer.build_strategy_audit_report(rows, winrate_rows=winrate_rows)
    assert report["strategies"][0]["win_rate"] == 0.6


def test_audit_report_attaches_category_metadata() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [_row("alpha")]
    report = analyzer.build_strategy_audit_report(
        rows, strategy_categories={"alpha": "trend", "beta": "reversion"}
    )
    assert report["strategies"][0]["category"] == "trend"


def test_audit_report_status_warn_when_hold_ratio_high() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [_row("alpha", actionability="hold") for _ in range(8)] + [
        _row("alpha", actionability="actionable") for _ in range(2)
    ]
    thresholds = DiagnosticThresholds(hold_warn_threshold=0.5)
    report = analyzer.build_strategy_audit_report(rows, thresholds=thresholds)
    alpha = report["strategies"][0]
    assert alpha["status"] == "warn"
    assert "high_hold_ratio" in alpha["warnings"]
    assert alpha["recent_issue"] == "high_hold_ratio"


def test_audit_report_status_warn_when_blocked_ratio_above_50pct() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [_row("alpha", actionability="blocked") for _ in range(6)] + [
        _row("alpha", actionability="actionable") for _ in range(4)
    ]
    report = analyzer.build_strategy_audit_report(rows)
    alpha = report["strategies"][0]
    assert "high_blocked_ratio" in alpha["warnings"]
    assert alpha["status"] == "warn"


def test_audit_report_status_warn_when_avg_confidence_low() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [_row("alpha", confidence=0.3, actionability="actionable") for _ in range(5)]
    thresholds = DiagnosticThresholds(confidence_warn_threshold=0.45)
    report = analyzer.build_strategy_audit_report(rows, thresholds=thresholds)
    alpha = report["strategies"][0]
    assert "low_avg_confidence" in alpha["warnings"]


def test_audit_report_status_ok_when_all_metrics_pass() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [
        _row("alpha", confidence=0.7, actionability="actionable") for _ in range(10)
    ]
    report = analyzer.build_strategy_audit_report(rows)
    alpha = report["strategies"][0]
    assert alpha["status"] == "ok"
    assert alpha["warnings"] == []
    assert alpha["recent_issue"] is None


def test_audit_report_top_level_metadata() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    rows = [_row("alpha"), _row("beta")]
    report = analyzer.build_strategy_audit_report(
        rows, scope="confirmed", symbol="XAUUSD", timeframe="H1"
    )
    assert report["rows_analyzed"] == 2
    assert report["scope"] == "confirmed"
    assert report["symbol"] == "XAUUSD"
    assert report["timeframe"] == "H1"
    assert "thresholds" in report
    assert len(report["strategies"]) == 2


def test_audit_report_empty_rows_returns_zero_payload() -> None:
    analyzer = SignalDiagnosticsAnalyzer()
    report = analyzer.build_strategy_audit_report([])
    assert report["rows_analyzed"] == 0
    assert report["strategies"] == []
