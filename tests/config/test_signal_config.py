from __future__ import annotations

import src.config.signal as signal_config


def test_signal_config_parses_session_and_execution_overrides(monkeypatch):
    merged = {
        "signal": {
            "auto_trade_enabled": "true",
            "allowed_sessions": "london,newyork",
            "max_spread_points": "55",
        },
        "regime": {"min_affinity_skip": "0.25"},
        "voting": {
            "enabled": "true",
            "consensus_threshold": "0.45",
            "min_quorum": "3",
            "disagreement_penalty": "0.4",
        },
        "circuit_breaker": {
            "max_consecutive_failures": "5",
            "circuit_auto_reset_minutes": "20",
        },
        "contract_sizes": {"XAUUSD": "100", "default": "10"},
        "session_spread_limits": {"asia": "30", "newyork": "48"},
        "strategy_sessions": {
            "rsi_reversion": "asia,london",
            "sma_trend": "london,newyork",
        },
        "market_structure": {
            "market_structure_enabled": "true",
            "market_structure_lookback_bars": "500",
            "market_structure_open_range_minutes": "45",
            "market_structure_compression_window_bars": "8",
            "market_structure_reference_window_bars": "30",
        },
        "execution_costs": {
            "max_spread_to_stop_ratio": "0.25",
        },
    }

    monkeypatch.setattr(signal_config, "get_merged_config", lambda name: merged)
    signal_config.get_signal_config.cache_clear()
    try:
        cfg = signal_config.get_signal_config()
    finally:
        signal_config.get_signal_config.cache_clear()

    assert cfg.min_affinity_skip == 0.25
    assert cfg.voting_enabled is True
    assert cfg.voting_consensus_threshold == 0.45
    assert cfg.voting_min_quorum == 3
    assert cfg.max_consecutive_failures == 5
    assert cfg.circuit_auto_reset_minutes == 20
    assert cfg.contract_size_map["XAUUSD"] == 100.0
    assert cfg.session_spread_limits["asia"] == 30.0
    assert cfg.strategy_sessions["rsi_reversion"] == ["asia", "london"]
    assert cfg.market_structure_enabled is True
    assert cfg.market_structure_lookback_bars == 500
    assert cfg.market_structure_open_range_minutes == 45
    assert cfg.market_structure_compression_window_bars == 8
    assert cfg.market_structure_reference_window_bars == 30
    assert cfg.max_spread_to_stop_ratio == 0.25
