from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


@dataclass(frozen=True)
class SignalEvent:
    """Published by SignalRuntime whenever a signal state transition is emitted.

    Any module can subscribe via SignalRuntime.add_signal_listener().
    The signal module itself has no knowledge of subscribers.
    """

    symbol: str
    timeframe: str
    strategy: str
    direction: str      # buy / sell / hold
    confidence: float
    signal_state: str   # confirmed_buy, confirmed_sell, confirmed_cancelled,
                        # armed_buy, armed_sell, preview_buy, preview_sell, cancelled
    scope: str          # "confirmed" (bar closed) / "intrabar" (in-progress bar)
    indicators: Dict[str, Dict[str, float]]
    metadata: Dict[str, Any]
    generated_at: datetime
    signal_id: str = ""     # populated after persist; empty for non-persisted events
    reason: str = ""


@dataclass(frozen=True)
class SignalContext:
    symbol: str
    timeframe: str
    strategy: str
    indicators: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    # HTF 指标：{target_tf: {indicator_name: {field: value}}}
    # 例: {"H1": {"adx14": {"adx": 28.5}, "ema50": {"ema": 2650.0}}}
    htf_indicators: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)
    # 经济事件行情影响预测（来自 MarketImpactAnalyzer）
    event_impact_forecast: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class SignalDecision:
    strategy: str
    symbol: str
    timeframe: str
    direction: str
    confidence: float
    reason: str
    used_indicators: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "confidence": self.confidence,
            "reason": self.reason,
            "used_indicators": list(self.used_indicators),
            "timestamp": self.timestamp.isoformat(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SignalRecord:
    generated_at: datetime
    signal_id: str
    symbol: str
    timeframe: str
    strategy: str
    direction: str
    confidence: float
    reason: str
    used_indicators: List[str] = field(default_factory=list)
    indicators_snapshot: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_decision(
        cls,
        decision: SignalDecision,
        indicators_snapshot: Dict[str, Any],
        metadata: Dict[str, Any] | None = None,
    ) -> "SignalRecord":
        return cls(
            generated_at=decision.timestamp,
            signal_id=uuid4().hex,
            symbol=decision.symbol,
            timeframe=decision.timeframe,
            strategy=decision.strategy,
            direction=decision.direction,
            confidence=float(decision.confidence),
            reason=decision.reason,
            used_indicators=list(decision.used_indicators),
            indicators_snapshot=dict(indicators_snapshot),
            metadata=dict(metadata or decision.metadata),
        )

    def to_row(self) -> tuple:
        return (
            self.generated_at,
            self.signal_id,
            self.symbol,
            self.timeframe,
            self.strategy,
            self.direction,
            self.confidence,
            self.reason,
            list(self.used_indicators),
            dict(self.indicators_snapshot),
            dict(self.metadata),
        )
