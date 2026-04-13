from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class StrategyCapability:
    """策略能力快照（运行时 + 回测共享）.

    - valid_scopes: 该策略支持的 scope，例如 ("intrabar", "confirmed")
    - needed_indicators: 决策依赖的指标名清单（有序去重）
    - needs_intrabar: 是否声明了 intrabar scope
    - needs_htf: 是否声明了 HTF 需求（strategy.htf_required_indicators 非空）
    """

    name: str
    valid_scopes: tuple[str, ...]
    needed_indicators: tuple[str, ...]
    needs_intrabar: bool
    needs_htf: bool
    regime_affinity: dict[str, float]
    htf_requirements: dict[str, str]

    @classmethod
    def from_contract(cls, raw: Mapping[str, Any]) -> "StrategyCapability":
        """Build a normalized capability snapshot from a contract payload."""
        return cls(
            name=str(raw.get("name") or ""),
            valid_scopes=tuple(str(scope) for scope in raw.get("valid_scopes", ())),
            needed_indicators=tuple(str(item) for item in raw.get("needed_indicators", ())),
            needs_intrabar=bool(raw.get("needs_intrabar")),
            needs_htf=bool(raw.get("needs_htf")),
            regime_affinity=dict(raw.get("regime_affinity") or {}),
            htf_requirements=dict(raw.get("htf_requirements") or {}),
        )

    def as_contract(self) -> dict[str, Any]:
        """Return the canonical strategy capability contract used by runtime/backtest.

        The returned fields are exactly the shared contract expected by
        module-policy handoff and diagnostics.
        """
        return {
            "name": self.name,
            "valid_scopes": list(self.valid_scopes),
            "needed_indicators": list(self.needed_indicators),
            "needs_intrabar": bool(self.needs_intrabar),
            "needs_htf": bool(self.needs_htf),
            "regime_affinity": dict(self.regime_affinity),
            "htf_requirements": dict(self.htf_requirements),
        }
