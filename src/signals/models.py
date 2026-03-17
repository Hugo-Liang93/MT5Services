from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4


@dataclass(frozen=True)
class SignalContext:
    symbol: str
    timeframe: str
    strategy: str
    indicators: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalDecision:
    strategy: str
    symbol: str
    timeframe: str
    action: str
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
            "action": self.action,
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
    action: str
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
            action=decision.action,
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
            self.action,
            self.confidence,
            self.reason,
            list(self.used_indicators),
            dict(self.indicators_snapshot),
            dict(self.metadata),
        )
