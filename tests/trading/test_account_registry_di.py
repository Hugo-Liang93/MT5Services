"""TradingAccountRegistry 配置注入回归测试（ADR-006 对齐）。

设计原则：
- registry 不允许在内部调全局 get_risk_config / get_economic_config，
  必须由装配层显式注入（构造参数）
- 测试可直接传 stub config 验证依赖；不需要 monkeypatch 全局函数
- 同一进程多个 registry 实例可持有不同 config（为同进程多账户场景铺路）
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from src.trading.runtime.registry import TradingAccountRegistry


def _stub_settings(alias: str = "demo_main") -> SimpleNamespace:
    return SimpleNamespace(
        instance_name=alias,
        account_alias=alias,
        account_label=alias,
        mt5_login=12345,
        mt5_password="pwd",
        mt5_server="Demo-Server",
        mt5_path="C:/MT5/terminal64.exe",
        timezone="UTC",
        enabled=True,
    )


def _stub_risk_config(daily_loss_limit_pct: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        daily_loss_limit_pct=daily_loss_limit_pct,
        max_trades_per_day=20,
        max_volume_per_order=0.1,
        allowed_sessions="asia,london,new_york",
        max_positions_per_symbol=None,
        max_open_positions_total=None,
        max_pending_orders_per_symbol=None,
        max_volume_per_symbol=None,
        max_net_lots_per_symbol=None,
        max_trades_per_hour=None,
        margin_safety_factor=1.2,
        market_order_protection="sl",
    )


def _stub_economic_config() -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        trade_guard_enabled=False,
        trade_guard_mode="warn",
        trade_guard_lookahead_minutes=180,
        trade_guard_lookback_minutes=0,
        trade_guard_importance_min=2,
        trade_guard_block_importance_min=3,
        trade_guard_warn_pre_buffer_minutes=15,
        trade_guard_warn_post_buffer_minutes=10,
        trade_guard_calendar_health_mode="warn_only",
        trade_guard_provider_failure_threshold=3,
        trade_guard_relevance_filter_enabled=True,
        gold_impact_keywords="NFP,FOMC",
        market_impact_symbols=["XAUUSD"],
    )


def test_registry_requires_risk_and_economic_config_as_kwargs() -> None:
    """ADR-006: risk_config / economic_config 必须由构造参数注入。"""
    # 缺 risk_config → TypeError
    with pytest.raises(TypeError, match="risk_config"):
        TradingAccountRegistry(
            settings=_stub_settings(),
            economic_config=_stub_economic_config(),
        )
    # 缺 economic_config → TypeError
    with pytest.raises(TypeError, match="economic_config"):
        TradingAccountRegistry(
            settings=_stub_settings(),
            risk_config=_stub_risk_config(),
        )


def test_registry_stores_injected_configs_without_global_calls(monkeypatch) -> None:
    """构造后内部 _risk_config / _economic_config 持有传入实例，不再调全局加载。"""
    # 探测：如果代码内部还在调全局函数，monkeypatch 会让它返回 sentinel
    # 测试断言 registry 拿到的是注入的 stub 而不是 sentinel
    sentinel_risk = SimpleNamespace(daily_loss_limit_pct=999.0)
    sentinel_econ = SimpleNamespace(enabled=False)
    monkeypatch.setattr(
        "src.config.get_risk_config", lambda: sentinel_risk, raising=False
    )
    monkeypatch.setattr(
        "src.config.get_economic_config", lambda: sentinel_econ, raising=False
    )

    risk = _stub_risk_config(daily_loss_limit_pct=3.0)
    econ = _stub_economic_config()
    registry = TradingAccountRegistry(
        settings=_stub_settings(),
        risk_config=risk,
        economic_config=econ,
    )

    # 构造完直接持有注入的实例（is 同一对象）
    assert registry._risk_config is risk
    assert registry._economic_config is econ


def test_registry_isolates_configs_across_instances() -> None:
    """两个 registry 实例可持有不同 config，证明配置不再通过全局共享。"""
    risk_a = _stub_risk_config(daily_loss_limit_pct=2.0)
    risk_b = _stub_risk_config(daily_loss_limit_pct=10.0)
    econ = _stub_economic_config()

    reg_a = TradingAccountRegistry(
        settings=_stub_settings("demo_main"),
        risk_config=risk_a,
        economic_config=econ,
    )
    reg_b = TradingAccountRegistry(
        settings=_stub_settings("live_main"),
        risk_config=risk_b,
        economic_config=econ,
    )

    assert reg_a._risk_config.daily_loss_limit_pct == 2.0
    assert reg_b._risk_config.daily_loss_limit_pct == 10.0
    # 两个 registry 持有的 risk_config 是不同实例
    assert reg_a._risk_config is not reg_b._risk_config


def test_registry_propagates_injected_configs_to_pre_trade_risk_service(
    monkeypatch,
) -> None:
    """get_trading_service() 构造的 PreTradeRiskService 必须用注入的 config。"""
    captured: dict[str, Any] = {}

    class _StubAccountClient:
        def __init__(self, settings):
            captured["account_settings"] = settings

    class _StubTradingClient:
        def __init__(self, settings):
            pass

    class _StubTradingService:
        def __init__(
            self, *, client, account_client, pre_trade_risk_service
        ):
            captured["pre_trade_risk"] = pre_trade_risk_service

    class _StubPreTradeRiskService:
        def __init__(
            self, *, economic_calendar_service, account_service, settings, risk_settings
        ):
            captured["risk_settings"] = risk_settings
            captured["economic_settings"] = settings

    monkeypatch.setattr(
        "src.trading.runtime.registry.MT5AccountClient", _StubAccountClient
    )
    monkeypatch.setattr(
        "src.trading.runtime.registry.MT5TradingClient", _StubTradingClient
    )
    monkeypatch.setattr(
        "src.trading.runtime.registry.TradingService", _StubTradingService
    )
    monkeypatch.setattr(
        "src.trading.runtime.registry.PreTradeRiskService", _StubPreTradeRiskService
    )

    risk = _stub_risk_config(daily_loss_limit_pct=7.5)
    econ = _stub_economic_config()
    registry = TradingAccountRegistry(
        settings=_stub_settings(),
        risk_config=risk,
        economic_config=econ,
    )

    registry.get_trading_service()

    # PreTradeRiskService 收到的 risk_settings / economic_settings 必须是注入的实例
    assert captured["risk_settings"] is risk
    assert captured["economic_settings"] is econ
