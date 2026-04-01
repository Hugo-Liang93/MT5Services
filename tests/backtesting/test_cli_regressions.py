"""Regression checks for backtesting CLI wiring.

These tests intentionally perform lightweight source-level assertions so they
remain stable in minimal environments where optional runtime dependencies are
not installed.
"""

from __future__ import annotations

from pathlib import Path


def _cli_source() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return (repo_root / "src" / "backtesting" / "cli.py").read_text(encoding="utf-8")


def test_cmd_optimize_loads_backtest_defaults_for_regime_sizing() -> None:
    src = _cli_source()
    assert "from .config import get_backtest_defaults" in src
    assert "ini_defaults = get_backtest_defaults()" in src
    assert 'regime_tp_trending=ini_defaults.get("regime_tp_trending", 1.20)' in src
    assert 'regime_sl_uncertain=ini_defaults.get("regime_sl_uncertain", 1.00)' in src


def test_compare_tf_rebuilds_components_per_timeframe() -> None:
    src = _cli_source()
    assert "for timeframe in timeframes:" in src
    assert "components = _build_components(args)" in src
    assert "_cleanup_components(components)" in src


def test_cli_loads_strategy_scope_overrides_from_signal_config() -> None:
    src = _cli_source()
    assert "def _load_strategy_scope_overrides()" in src
    assert "strategy_timeframes, strategy_sessions = _load_strategy_scope_overrides()" in src
    assert "strategy_timeframes=strategy_timeframes" in src
    assert "strategy_sessions=strategy_sessions" in src
