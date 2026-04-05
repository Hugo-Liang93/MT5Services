"""config.py 测试：INI 配置加载 + BacktestConfig.from_flat 映射。"""

from __future__ import annotations

import configparser
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import patch

import pytest

from src.backtesting.config import get_backtest_defaults
from src.backtesting.models import (
    BacktestConfig,
    ConfidenceConfig,
    FilterConfig,
    PositionConfig,
    RiskConfig,
)


class TestGetBacktestDefaults:
    def test_returns_dict(self) -> None:
        result = get_backtest_defaults()
        assert isinstance(result, dict)

    def test_loads_from_ini(self) -> None:
        """从实际 backtest.ini 加载时应包含 initial_balance。"""
        result = get_backtest_defaults()
        if result:
            # backtest.ini 存在时应有这些字段
            assert "initial_balance" in result or "warmup_bars" in result

    def test_parses_confidence_fields(self) -> None:
        """应加载 [confidence] section 中的 htf_alignment_boost 等新字段。"""
        result = get_backtest_defaults()
        if "enable_regime_affinity" in result:
            # 如果加载了 confidence section，新字段也应该在
            assert "htf_alignment_boost" in result or "bars_to_evaluate" in result

    def test_custom_ini_file(self, tmp_path: Any) -> None:
        """使用自定义 INI 内容测试解析。"""
        ini_content = """
[backtest]
default_initial_balance = 50000.0
min_confidence = 0.70

[filters]
enabled = false
allowed_sessions = asia,london

[confidence]
enable_regime_affinity = false
htf_alignment_boost = 1.25
htf_conflict_penalty = 0.60
bars_to_evaluate = 8
"""
        ini_file = tmp_path / "backtest.ini"
        ini_file.write_text(ini_content, encoding="utf-8")

        with patch("src.backtesting.config._CONFIG_DIR", str(tmp_path)):
            result = get_backtest_defaults()

        assert result["initial_balance"] == 50000.0
        assert result["min_confidence"] == 0.70
        assert result["filters_enabled"] is False
        assert result["filter_allowed_sessions"] == "asia,london"
        assert result["enable_regime_affinity"] is False
        assert result["htf_alignment_boost"] == 1.25
        assert result["htf_conflict_penalty"] == 0.60
        assert result["bars_to_evaluate"] == 8


class TestBacktestConfigFromFlat:
    def test_core_fields_pass_through(self) -> None:
        config = BacktestConfig.from_flat(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 2, 1, tzinfo=timezone.utc),
            initial_balance=20000.0,
        )
        assert config.symbol == "XAUUSD"
        assert config.initial_balance == 20000.0

    def test_filter_fields_mapped(self) -> None:
        config = BacktestConfig.from_flat(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 2, 1, tzinfo=timezone.utc),
            filters_enabled=False,
            filter_allowed_sessions="asia",
            filter_volatility_spike_multiplier=3.0,
        )
        assert config.filters.enabled is False
        assert config.filters.allowed_sessions == "asia"
        assert config.filters.volatility_spike_multiplier == 3.0

    def test_risk_fields_mapped(self) -> None:
        config = BacktestConfig.from_flat(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 2, 1, tzinfo=timezone.utc),
            max_positions=5,
            commission_per_lot=7.0,
            daily_loss_limit_pct=10.0,
        )
        assert config.risk.max_positions == 5
        assert config.risk.commission_per_lot == 7.0
        assert config.risk.daily_loss_limit_pct == 10.0

    def test_confidence_fields_mapped(self) -> None:
        config = BacktestConfig.from_flat(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 2, 1, tzinfo=timezone.utc),
            min_confidence=0.70,
            enable_regime_affinity=False,
            htf_alignment_boost=1.25,
            htf_conflict_penalty=0.60,
            bars_to_evaluate=10,
        )
        assert config.confidence.min_confidence == 0.70
        assert config.confidence.enable_regime_affinity is False
        assert config.confidence.htf_alignment_boost == 1.25
        assert config.confidence.htf_conflict_penalty == 0.60
        assert config.confidence.bars_to_evaluate == 10

    def test_default_sub_configs(self) -> None:
        config = BacktestConfig(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 2, 1, tzinfo=timezone.utc),
        )
        assert config.position == PositionConfig()
        assert config.risk == RiskConfig()
        assert config.filters == FilterConfig()
        assert config.confidence == ConfidenceConfig()

    def test_unknown_flat_key_raises(self) -> None:
        with pytest.raises(TypeError):
            BacktestConfig.from_flat(
                symbol="XAUUSD",
                timeframe="M5",
                start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                end_time=datetime(2025, 2, 1, tzinfo=timezone.utc),
                nonexistent_field=42,
            )
