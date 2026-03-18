from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

from .adapters import IndicatorSource
from .models import SignalContext, SignalDecision, SignalRecord
from .repository import SignalRepository
from .strategies import (
    BollingerBreakoutStrategy,
    MultiTimeframeConfirmStrategy,
    RsiReversionStrategy,
    SignalStrategy,
    SmaTrendStrategy,
)

logger = logging.getLogger(__name__)


class SignalModule:
    def __init__(
        self,
        indicator_source: IndicatorSource,
        strategies: Optional[Iterable[SignalStrategy]] = None,
        repository: Optional[SignalRepository] = None,
    ):
        self.indicator_source = indicator_source
        self.repository = repository
        self._strategies: dict[str, SignalStrategy] = {}
        default_strategies: Iterable[SignalStrategy] = strategies or (
            SmaTrendStrategy(),
            RsiReversionStrategy(),
            BollingerBreakoutStrategy(),
            MultiTimeframeConfirmStrategy(),
        )
        for strategy in default_strategies:
            self.register_strategy(strategy)

    def register_strategy(self, strategy: SignalStrategy) -> None:
        self._strategies[strategy.name] = strategy

    def list_strategies(self) -> list[str]:
        return sorted(self._strategies.keys())

    def strategy_requirements(self, strategy: str) -> tuple[str, ...]:
        strategy_impl = self._strategies.get(strategy)
        if strategy_impl is None:
            raise ValueError(f"unsupported signal strategy: {strategy}")
        requirements = getattr(strategy_impl, "required_indicators", ())
        return tuple(str(item) for item in requirements)

    def all_required_indicators(self) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for strategy_name in self.list_strategies():
            for indicator_name in self.strategy_requirements(strategy_name):
                if indicator_name in seen:
                    continue
                seen.add(indicator_name)
                ordered.append(indicator_name)
        return ordered

    def required_indicator_groups(self) -> list[tuple[str, ...]]:
        ordered: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for strategy_name in self.list_strategies():
            group = tuple(self.strategy_requirements(strategy_name))
            if not group or group in seen:
                continue
            seen.add(group)
            ordered.append(group)
        return ordered

    def evaluate(
        self,
        symbol: str,
        timeframe: str,
        strategy: str,
        indicators: Optional[Dict[str, Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        persist: bool = True,
    ) -> SignalDecision:
        strategy_impl = self._strategies.get(strategy)
        if strategy_impl is None:
            raise ValueError(f"unsupported signal strategy: {strategy}")

        indicator_payload = indicators or self.indicator_source.get_all_indicators(symbol, timeframe)
        context = SignalContext(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            indicators=indicator_payload,
            metadata=metadata or {},
        )
        decision = strategy_impl.evaluate(context)
        if persist:
            self._persist_signal(decision, indicator_payload, metadata)
        return decision

    def _persist_signal(
        self,
        decision: SignalDecision,
        indicators: Dict[str, Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[SignalRecord]:
        if self.repository is None:
            return None
        try:
            merged_metadata = dict(decision.metadata)
            if metadata:
                merged_metadata.update(metadata)
            record = SignalRecord.from_decision(
                decision,
                indicators_snapshot=indicators,
                metadata=merged_metadata,
            )
            self.repository.append(record)
            return record
        except Exception:
            logger.exception("Failed to persist signal event for %s/%s", decision.symbol, decision.strategy)
            return None

    def persist_decision(
        self,
        decision: SignalDecision,
        indicators: Dict[str, Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[SignalRecord]:
        return self._persist_signal(decision, indicators, metadata)

    def recent_signals(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        action: Optional[str] = None,
        scope: str = "confirmed",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        return self.repository.recent(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            action=action,
            scope=scope,
            limit=limit,
        )

    def summary(self, *, hours: int = 24, scope: str = "confirmed") -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        return self.repository.summary(hours=hours, scope=scope)

    def dispatch_operation(self, operation: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        payload = payload or {}
        handlers = {
            "evaluate": lambda: self.evaluate(
                symbol=payload["symbol"],
                timeframe=payload["timeframe"],
                strategy=payload["strategy"],
                indicators=payload.get("indicators"),
                metadata=payload.get("metadata"),
                persist=payload.get("persist", True),
            ).to_dict(),
            "strategies": self.list_strategies,
            "strategy_requirements": lambda: {
                name: list(self.strategy_requirements(name))
                for name in self.list_strategies()
            },
            "required_indicator_groups": lambda: [
                list(group) for group in self.required_indicator_groups()
            ],
            "available_indicators": lambda: [item.get("name") for item in self.indicator_source.list_indicators()],
            "recent": lambda: self.recent_signals(
                symbol=payload.get("symbol"),
                timeframe=payload.get("timeframe"),
                strategy=payload.get("strategy"),
                action=payload.get("action"),
                scope=payload.get("scope", "confirmed"),
                limit=payload.get("limit", 200),
            ),
            "summary": lambda: self.summary(hours=payload.get("hours", 24), scope=payload.get("scope", "confirmed")),
        }
        if operation not in handlers:
            raise ValueError(f"unsupported signal operation: {operation}")
        return handlers[operation]()
