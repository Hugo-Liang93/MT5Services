"""经济日历数据源注册表。

替代 EconomicCalendarService 中的硬编码 if/elif provider 分支，
支持任意数量的数据源通过 Protocol 注册。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from src.clients.economic_calendar import EconomicCalendarProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """经济日历 Provider 注册表。"""

    def __init__(self) -> None:
        self._providers: Dict[str, EconomicCalendarProvider] = {}

    def register(self, provider: EconomicCalendarProvider) -> None:
        """注册一个 provider。重复注册同名 provider 会覆盖。"""
        self._providers[provider.name] = provider
        logger.info(
            "Registered economic calendar provider: %s (configured=%s)",
            provider.name,
            provider.is_configured(),
        )

    def get(self, name: str) -> Optional[EconomicCalendarProvider]:
        """按名称获取 provider。"""
        return self._providers.get(name)

    def all_names(self) -> List[str]:
        """所有已注册的 provider 名称。"""
        return list(self._providers.keys())

    def configured_names(self) -> List[str]:
        """所有已配置（API key 就绪）的 provider 名称。"""
        return [
            name
            for name, provider in self._providers.items()
            if provider.is_configured()
        ]

    def release_watch_names(self) -> List[str]:
        """支持 release_watch 且已配置的 provider 名称。"""
        return [
            name
            for name, provider in self._providers.items()
            if provider.is_configured() and provider.supports_release_watch()
        ]

    def __len__(self) -> int:
        return len(self._providers)

    def __contains__(self, name: str) -> bool:
        return name in self._providers
