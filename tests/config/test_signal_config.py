from __future__ import annotations

import src.config.signal as signal_config


def test_signal_config_parses_session_and_execution_overrides(monkeypatch):
    merged = {
        "signal": {
            "auto_trade_enabled": "true",
            "allowed_sessions": "london,newyork",
            "max_spread_points": "55",
            "max_concurrent_positions_per_symbol": "3",
            "session_transition_cooldown_minutes": "15",
        },
        "regime": {
            "min_affinity_skip": "0.25",
            "soft_regime_enabled": "true",
        },
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
            "session_momentum": "london,newyork",
        },
        "strategy_timeframes": {
            "rsi_reversion": "M1,M5",
            "supertrend": "M1,M5",
            "ema_ribbon": "M5,M15",
            "session_momentum": "M5,M15",
            "fake_breakout": "M5,M15",
            "squeeze_release": "M5,M15",
            "price_action_reversal": "M5,M15",
            "multi_timeframe_confirm": "M15,H1",
        },
        "voting_groups": {
            "trend_vote": "supertrend,ema_ribbon,macd_momentum,hma_cross,session_momentum",
            "reversion_vote": "rsi_reversion,stoch_rsi,williams_r,cci_reversion,price_action_reversal",
            "breakout_vote": "bollinger_breakout,donchian_breakout,keltner_bb_squeeze,squeeze_release,fake_breakout",
        },
        "voting_group.trend_vote": {
            "consensus_threshold": "0.45",
            "min_quorum": "2",
            "disagreement_penalty": "0.50",
        },
        "market_structure": {
            "market_structure_enabled": "true",
            "market_structure_lookback_bars": "500",
            "market_structure_m1_lookback_bars": "120",
            "market_structure_open_range_minutes": "45",
            "market_structure_compression_window_bars": "8",
            "market_structure_reference_window_bars": "30",
        },
        "position_management": {
            "end_of_day_close_enabled": "true",
            "end_of_day_close_hour_utc": "21",
            "end_of_day_close_minute_utc": "0",
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
    assert cfg.soft_regime_enabled is True
    assert cfg.voting_enabled is True
    assert cfg.voting_consensus_threshold == 0.45
    assert cfg.voting_min_quorum == 3
    assert cfg.max_consecutive_failures == 5
    assert cfg.circuit_auto_reset_minutes == 20
    assert cfg.max_concurrent_positions_per_symbol == 3
    assert cfg.session_transition_cooldown_minutes == 15
    assert cfg.contract_size_map["XAUUSD"] == 100.0
    assert cfg.session_spread_limits["asia"] == 30.0
    assert cfg.strategy_sessions["rsi_reversion"] == ["asia", "london"]
    assert cfg.strategy_sessions["session_momentum"] == ["london", "newyork"]
    assert cfg.strategy_timeframes["rsi_reversion"] == ["m1", "m5"]
    assert cfg.strategy_timeframes["ema_ribbon"] == ["m5", "m15"]
    assert cfg.strategy_timeframes["session_momentum"] == ["m5", "m15"]
    assert cfg.strategy_timeframes["fake_breakout"] == ["m5", "m15"]
    assert cfg.strategy_timeframes["squeeze_release"] == ["m5", "m15"]
    assert cfg.strategy_timeframes["price_action_reversal"] == ["m5", "m15"]
    assert cfg.strategy_timeframes["multi_timeframe_confirm"] == ["m15", "h1"]
    assert len(cfg.voting_group_configs) == 3
    trend_vote = next(
        item for item in cfg.voting_group_configs if item["name"] == "trend_vote"
    )
    assert "session_momentum" in trend_vote["strategies"]
    assert cfg.market_structure_enabled is True
    assert cfg.market_structure_lookback_bars == 500
    assert cfg.market_structure_m1_lookback_bars == 120
    assert cfg.market_structure_open_range_minutes == 45
    assert cfg.market_structure_compression_window_bars == 8
    assert cfg.market_structure_reference_window_bars == 30
    assert cfg.end_of_day_close_enabled is True
    assert cfg.end_of_day_close_hour_utc == 21
    assert cfg.end_of_day_close_minute_utc == 0
    assert cfg.max_spread_to_stop_ratio == 0.25
