from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class TradeIntent:
    symbol: str
    volume: float
    side: str
    order_kind: str = "market"
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    deviation: int = 20
    comment: str = ""
    magic: int = 0
    at_time: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "symbol": self.symbol,
            "volume": self.volume,
            "side": self.side,
            "order_kind": self.order_kind,
            "price": self.price,
            "sl": self.sl,
            "tp": self.tp,
            "deviation": self.deviation,
            "comment": self.comment,
            "magic": self.magic,
        }
        if self.at_time is not None:
            payload["at_time"] = self.at_time.isoformat()
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class RiskCheckResult:
    name: str
    verdict: str = "allow"
    reason: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict,
            "reason": self.reason,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class RiskAssessment:
    verdict: str
    blocked: bool
    reason: Optional[str]
    warnings: List[str] = field(default_factory=list)
    checks: List[RiskCheckResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "blocked": self.blocked,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "checks": [check.to_dict() for check in self.checks],
        }
