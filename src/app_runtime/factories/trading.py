from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from src.calendar import EconomicCalendarService
from src.calendar.read_only_provider import ReadOnlyEconomicCalendarProvider
from src.clients.economic_calendar import (
    FredCalendarClient,
    TradingEconomicsCalendarClient,
)
from src.clients.economic_calendar_registry import ProviderRegistry
from src.config import EconomicConfig, RiskConfig, load_mt5_settings
from src.trading.application.module import TradingModule
from src.trading.runtime.registry import TradingAccountRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradingComponents:
    economic_calendar_service: Any
    trade_registry: TradingAccountRegistry
    trade_module: TradingModule
    active_account_alias: str


def build_provider_registry(settings: EconomicConfig) -> ProviderRegistry:
    """根据配置构建 Provider 注册表。"""
    registry = ProviderRegistry()
    if settings.tradingeconomics_enabled:
        registry.register(TradingEconomicsCalendarClient(settings))
    if settings.fred_enabled:
        registry.register(FredCalendarClient(settings))
    # FMP — 延迟导入，仅在启用时加载
    if settings.fmp_enabled:
        try:
            from src.clients.fmp_calendar import FmpCalendarClient

            registry.register(FmpCalendarClient(settings))
        except ImportError:
            logger.warning(
                "FMP calendar client not available (src.clients.fmp_calendar)"
            )
    # Jin10 — 延迟导入，仅在启用时加载（无需 API Key）
    if settings.jin10_enabled:
        try:
            from src.clients.jin10_calendar import Jin10CalendarClient

            registry.register(Jin10CalendarClient(settings))
        except ImportError:
            logger.warning(
                "Jin10 calendar client not available (src.clients.jin10_calendar)"
            )
    # Alpha Vantage — 延迟导入，仅在启用时加载
    if settings.alphavantage_enabled:
        try:
            from src.clients.alphavantage_calendar import AlphaVantageClient

            registry.register(AlphaVantageClient(settings))
        except ImportError:
            logger.warning(
                "Alpha Vantage client not available (src.clients.alphavantage_calendar)"
            )
    return registry


def _resolve_main_target_instance_id(runtime_identity: Any | None) -> str | None:
    """§0dh P2：根据 runtime_identity 解析对应 group 的 main instance_id。

    与 RuntimeIdentity._build_runtime_identity 同口径——`"<environment>:<main_instance_name>"`。
    用于 executor 拓扑下 ReadOnlyEconomicCalendarProvider 读 main 写入的
    runtime_task_status 行。

    解析失败时返 None（read provider 退化为按 role=main 全局查，单 group 拓扑下仍命中）。
    """
    if runtime_identity is None:
        return None
    instance_name = getattr(runtime_identity, "instance_name", None)
    environment = getattr(runtime_identity, "environment", None)
    if not instance_name or not environment:
        return None
    try:
        from src.config.topology import find_topology_group_for_instance
    except ImportError:
        return None
    try:
        group = find_topology_group_for_instance(instance_name)
    except Exception:
        logger.warning(
            "ReadOnlyProvider target_instance_id 解析失败 (instance=%s)",
            instance_name,
            exc_info=True,
        )
        return None
    if group is None or not group.main:
        return None
    return f"{environment}:{group.main}"


def build_trading_components(
    storage_writer,
    economic_settings: EconomicConfig,
    risk_config: RiskConfig,
    runtime_identity=None,
    *,
    enable_calendar_sync: bool = True,
) -> TradingComponents:
    """构建交易域组件。

    risk_config 必需 — 由 builder 显式注入到 TradingAccountRegistry，
    替代之前在 registry 内部直接调 get_risk_config() 的全局耦合（ADR-006）。
    """
    if enable_calendar_sync:
        registry = build_provider_registry(economic_settings)
        economic_calendar_service = EconomicCalendarService(
            db_writer=storage_writer.db,
            settings=economic_settings,
            storage_writer=storage_writer,
            provider_registry=registry,
            runtime_identity=runtime_identity,
        )
    else:
        # §0dh P2：executor 拓扑下读 ReadOnlyProvider，必须按 main 实例的
        # instance_id 查 runtime_task_status（main 写入的行）。通过 topology
        # 解析 same group 的 main instance_name，按 RuntimeIdentity 同口径
        # 构造 main 的 instance_id（"<environment>:<instance_name>"）。
        target_instance_id = _resolve_main_target_instance_id(runtime_identity)
        economic_calendar_service = ReadOnlyEconomicCalendarProvider(
            db_writer=storage_writer.db,
            settings=economic_settings,
            runtime_identity=runtime_identity,
            target_instance_id=target_instance_id,
        )
    mt5_settings = load_mt5_settings()
    trade_registry = TradingAccountRegistry(
        settings=mt5_settings,
        economic_calendar_service=economic_calendar_service,
        risk_config=risk_config,
        economic_config=economic_settings,
    )
    trade_module = TradingModule(
        registry=trade_registry,
        db_writer=storage_writer.db,
        active_account_alias=mt5_settings.account_alias,
    )
    return TradingComponents(
        economic_calendar_service=economic_calendar_service,
        trade_registry=trade_registry,
        trade_module=trade_module,
        active_account_alias=mt5_settings.account_alias,
    )
