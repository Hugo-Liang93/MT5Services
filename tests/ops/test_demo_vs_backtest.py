"""Regression + unit tests for src/ops/cli/demo_vs_backtest.py.

历史 bug（2026-04-30 评估发现）：
- demo SQL 用 `entry_time` / `realized_pnl` —— trade_outcomes 表无此字段
- demo SQL 用 `signal_events.signal_state` —— signals 表只有 `direction`
- backtest 端读 `summary['per_strategy']` —— fetch_evaluation_summary 返回的是
  `by_strategy_direction`，且无 total_trades/profit_factor 字段（应从
  backtest_runs.metrics_by_strategy JSONB 取）
- pf_drift 比较 demo `price_change`（raw 价差）vs backtest `total_pnl`（USD），
  单位不一致 —— 数学有效但语义错误，应移除而非加 fallback

修复后：sentinel grep 锁定 SQL 字段对齐 + 单元测 drift 计算逻辑。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

from src.ops.cli import demo_vs_backtest as cli

# ── SQL 字段对齐 sentinel ────────────────────────────────────────────


def test_demo_sql_does_not_reference_nonexistent_entry_time() -> None:
    """trade_outcomes 表无 entry_time；正确字段是 recorded_at（close 时间）。"""
    src = inspect.getsource(cli)
    assert (
        "entry_time BETWEEN" not in src
    ), "trade_outcomes.entry_time 不存在；应用 recorded_at（参 schema/trade_outcomes.py）"


def test_demo_sql_does_not_reference_nonexistent_realized_pnl() -> None:
    """trade_outcomes 表无 realized_pnl；正确字段是 won (bool) + price_change。"""
    src = inspect.getsource(cli)
    assert (
        "realized_pnl" not in src
    ), "trade_outcomes.realized_pnl 不存在；应用 won + price_change"


def test_demo_sql_does_not_reference_nonexistent_signal_state() -> None:
    """signal_events 表无 signal_state；正确字段是 direction IN ('buy','sell','hold')。"""
    src = inspect.getsource(cli)
    assert (
        "signal_state" not in src
    ), "signal_events.signal_state 不存在；应用 direction IN ('buy','sell')"


def test_demo_sql_uses_correct_trade_outcomes_fields() -> None:
    """SQL 应使用 recorded_at + won 这些表里真实存在的字段。"""
    src = inspect.getsource(cli)
    # recorded_at 是 trade_outcomes 的 PK + hypertable 时间分区列
    assert "recorded_at" in src, "应用 recorded_at 作为时间过滤字段"
    assert "won" in src, "应用 won 字段（boolean，True=盈利）"


def test_demo_sql_filters_signal_direction() -> None:
    """signal_events.direction IN ('buy','sell') 是真实可用的过滤。"""
    src = inspect.getsource(cli)
    # 接受单引号或双引号
    assert (
        "direction IN ('buy', 'sell')" in src or 'direction IN ("buy", "sell")' in src
    ), "应过滤 direction IN ('buy', 'sell') 排除 hold"


# ── backtest 端：从 metrics_by_strategy JSONB 取，不从 evaluation_summary ──


def test_backtest_loader_reads_metrics_by_strategy_jsonb() -> None:
    """backtest_runs.metrics_by_strategy 已含 total_trades/win_rate/profit_factor
    （persistence/repositories/backtest_repo.py:92），CLI 应从此处读。
    fetch_evaluation_summary 返回的是 by_strategy_direction，且无最终 metrics 字段。
    """
    src = inspect.getsource(cli)
    assert (
        "metrics_by_strategy" in src
    ), "应从 backtest_runs.metrics_by_strategy JSONB 取每策略最终 metrics"


def test_backtest_loader_does_not_read_nonexistent_per_strategy_key() -> None:
    """fetch_evaluation_summary 返回 dict 里没有 per_strategy 键，
    旧代码读它永远拿到空（每个 backtest_trades=0）。"""
    src = inspect.getsource(cli)
    assert (
        "per_strategy" not in src
    ), "fetch_evaluation_summary 不返回 per_strategy；应改读 metrics_by_strategy"


# ── pf_drift 移除：单位不一致不能比 ────────────────────────────────


def test_pf_drift_removed() -> None:
    """demo price_change 是 raw 价差（无 lot/contract），backtest total_pnl 是 USD；
    比例 drift 单位不可比。改用 win_rate + trade_count drift（unit-free）。"""
    src = inspect.getsource(cli)
    assert (
        "pf_drift" not in src
    ), "pf_drift 比较单位不一致；改用 win_rate_drift + trade_count_drift"


def test_win_rate_drift_added() -> None:
    """新 drift：abs(demo_win_rate - backtest_win_rate)，单位均为 0~1 比率。"""
    src = inspect.getsource(cli)
    assert "win_rate_drift" in src, "应有 win_rate_drift 度量"


# ── _build_report 单元测 ─────────────────────────────────────────────


def _make_aggregate(
    strategy: str,
    *,
    bt_trades: int = 100,
    bt_wr: float = 0.55,
    bt_pf: float = 1.30,
    bt_pnl: float = 200.0,
    demo_signal: int = 100,
    demo_actionable: int = 90,
    demo_trades: int = 95,
    demo_wins: int = 50,
    demo_signed_change: float = 100.0,
) -> "cli.StrategyAggregate":
    agg = cli.StrategyAggregate(strategy=strategy)
    agg.backtest_trades = bt_trades
    agg.backtest_win_rate = bt_wr
    agg.backtest_profit_factor = bt_pf
    agg.backtest_total_pnl = bt_pnl
    agg.demo_signal_count = demo_signal
    agg.demo_actionable_count = demo_actionable
    agg.demo_trades = demo_trades
    agg.demo_win_rate = demo_wins / demo_trades if demo_trades else 0.0
    agg.demo_signed_price_change = demo_signed_change
    return agg


def _make_config(strategies=("alpha",), **overrides) -> "cli.ReconciliationConfig":
    base = dict(
        backtest_run_id="run_x",
        demo_window_start=datetime(2026, 4, 1, tzinfo=timezone.utc),
        demo_window_end=datetime(2026, 4, 30, tzinfo=timezone.utc),
        strategies=tuple(strategies),
        signal_count_drift_alarm=0.30,
        actionability_rate_warn=0.50,
        trades_drift_alarm=0.30,
        win_rate_drift_alarm=0.15,
    )
    base.update(overrides)
    return cli.ReconciliationConfig(**base)


def test_report_includes_signed_price_change_for_demo() -> None:
    """demo 的 PnL 字段应明确为 signed_price_change（raw price diff 加方向号）。"""
    cfg = _make_config()
    aggs = {"alpha": _make_aggregate("alpha")}
    report = cli._build_report(cfg, aggs)
    payload = report.aggregates[0]
    assert (
        "signed_price_change" in payload["demo"]
    ), "demo payload 应含 signed_price_change 而非 total_pnl(USD)，避免单位误读"


def test_report_does_not_emit_pf_drift_alarm() -> None:
    """旧 pf_drift alarm 逻辑应被移除。"""
    cfg = _make_config()
    aggs = {"alpha": _make_aggregate("alpha", bt_pnl=200.0, demo_signed_change=10.0)}
    report = cli._build_report(cfg, aggs)
    metrics = {alarm["metric"] for alarm in report.alarms}
    assert "pf_drift" not in metrics, "pf_drift 已废弃"


def test_report_alarms_on_win_rate_drift() -> None:
    """demo WR 比 backtest WR 偏离过多 → ALARM。"""
    cfg = _make_config(win_rate_drift_alarm=0.10)
    aggs = {
        "alpha": _make_aggregate(
            "alpha",
            bt_wr=0.55,
            demo_trades=100,
            demo_wins=30,  # WR=0.30 vs 0.55 → drift=0.25 > 0.10
        )
    }
    report = cli._build_report(cfg, aggs)
    metrics = {alarm["metric"] for alarm in report.alarms}
    assert "win_rate_drift" in metrics


def test_report_alarms_on_trades_drift() -> None:
    cfg = _make_config(trades_drift_alarm=0.30)
    aggs = {
        "alpha": _make_aggregate(
            "alpha", bt_trades=100, demo_trades=40
        )  # drift=0.60 > 0.30
    }
    report = cli._build_report(cfg, aggs)
    metrics = {alarm["metric"] for alarm in report.alarms}
    assert "trades_drift" in metrics


def test_report_warns_on_low_actionability() -> None:
    cfg = _make_config(actionability_rate_warn=0.50)
    aggs = {
        "alpha": _make_aggregate(
            "alpha", demo_signal=100, demo_actionable=20
        )  # 0.20 < 0.50
    }
    report = cli._build_report(cfg, aggs)
    metrics = {w["metric"] for w in report.warnings}
    assert "actionability_rate" in metrics


def test_report_clean_when_drifts_within_tolerance() -> None:
    cfg = _make_config()
    aggs = {
        "alpha": _make_aggregate(
            "alpha",
            bt_trades=100,
            demo_trades=95,  # drift=0.05
            bt_wr=0.55,
            demo_wins=53,  # WR≈0.558 → drift=0.008
            demo_signal=100,
            demo_actionable=90,  # 0.90 actionability
        )
    }
    report = cli._build_report(cfg, aggs)
    assert report.alarms == []
    assert report.warnings == []
    assert report.exit_code == 0
