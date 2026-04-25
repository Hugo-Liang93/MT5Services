from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from .sessions import normalize_session_name


class StrategyDeploymentStatus(str, Enum):
    CANDIDATE = "candidate"
    DEMO_VALIDATION = "demo_validation"
    ACTIVE_GUARDED = "active_guarded"
    ACTIVE = "active"


@dataclass(frozen=True)
class StrategyDeployment:
    """策略部署合同。

    该合同只承载部署语义，不复制策略逻辑：
    - status 控制 live/runtime 的参与方式
    - locked_timeframes / locked_sessions 收口 tf_specific 护栏
    - guarded 策略通过 min_final_confidence / max_live_positions /
      require_pending_entry 等硬约束限制 live 行为
    """

    name: str
    status: StrategyDeploymentStatus = StrategyDeploymentStatus.ACTIVE
    locked_timeframes: tuple[str, ...] = field(default_factory=tuple)
    locked_sessions: tuple[str, ...] = field(default_factory=tuple)
    min_final_confidence: float | None = None
    max_live_positions: int | None = None
    require_pending_entry: bool = False
    paper_shadow_required: bool = False
    robustness_tier: str | None = None
    research_provenance: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "locked_timeframes": list(self.locked_timeframes),
            "locked_sessions": list(self.locked_sessions),
            "min_final_confidence": self.min_final_confidence,
            "max_live_positions": self.max_live_positions,
            "require_pending_entry": self.require_pending_entry,
            "paper_shadow_required": self.paper_shadow_required,
            "robustness_tier": self.robustness_tier,
            "research_provenance": self.research_provenance,
        }

    @classmethod
    def from_dict(
        cls,
        name: str,
        payload: Mapping[str, Any] | None,
    ) -> "StrategyDeployment":
        raw = dict(payload or {})
        if "status" not in raw:
            raise ValueError(
                f"strategy_deployment.{name}.status is required; "
                "live eligibility must be declared explicitly"
            )
        status = raw.get("status")
        return cls(
            name=str(name).strip(),
            status=(
                status
                if isinstance(status, StrategyDeploymentStatus)
                else StrategyDeploymentStatus(str(status).strip().lower())
            ),
            locked_timeframes=_normalize_timeframes(raw.get("locked_timeframes")),
            locked_sessions=_normalize_sessions(raw.get("locked_sessions")),
            min_final_confidence=_maybe_float(raw.get("min_final_confidence")),
            max_live_positions=_maybe_int(raw.get("max_live_positions")),
            require_pending_entry=_coerce_bool(raw.get("require_pending_entry"), False),
            paper_shadow_required=_coerce_bool(raw.get("paper_shadow_required"), False),
            robustness_tier=_maybe_text(raw.get("robustness_tier")),
            research_provenance=_maybe_text(raw.get("research_provenance")),
        )

    def allows_runtime_evaluation(self) -> bool:
        return self.status is not StrategyDeploymentStatus.CANDIDATE

    def allows_live_execution(self) -> bool:
        return self.status in {
            StrategyDeploymentStatus.ACTIVE,
            StrategyDeploymentStatus.ACTIVE_GUARDED,
        }

    def allows_demo_validation(self) -> bool:
        """Strategy is eligible for装配 on demo-environment instances.

        覆盖 ACTIVE / ACTIVE_GUARDED / DEMO_VALIDATION 三档（参考 ADR-010）。
        live instance 装配仅 ACTIVE / ACTIVE_GUARDED；demo instance 额外含 DEMO_VALIDATION 候选。
        """
        return self.status in {
            StrategyDeploymentStatus.ACTIVE,
            StrategyDeploymentStatus.ACTIVE_GUARDED,
            StrategyDeploymentStatus.DEMO_VALIDATION,
        }

    def is_guarded(self) -> bool:
        return self.status is StrategyDeploymentStatus.ACTIVE_GUARDED

    def effective_min_confidence(
        self,
        *,
        timeframe_baseline: float | None,
        global_min_confidence: float,
    ) -> float | None:
        if self.min_final_confidence is not None:
            return float(self.min_final_confidence)
        if not self.is_guarded():
            return None
        baseline = (
            float(timeframe_baseline)
            if timeframe_baseline is not None
            else float(global_min_confidence)
        )
        return max(0.50, baseline + 0.05)


def normalize_strategy_deployments(
    deployments: Mapping[str, Any] | None,
) -> dict[str, StrategyDeployment]:
    normalized: dict[str, StrategyDeployment] = {}
    for raw_name, raw_payload in (deployments or {}).items():
        name = str(raw_name).strip()
        if not name:
            continue
        if isinstance(raw_payload, StrategyDeployment):
            normalized[name] = raw_payload
        else:
            normalized[name] = StrategyDeployment.from_dict(name, raw_payload)
    return normalized


def validate_strategy_deployments(
    *,
    deployments: Mapping[str, StrategyDeployment] | None,
    known_strategies: Sequence[str],
    strategy_timeframes_policy: Mapping[str, Sequence[str]] | None,
    strategy_sessions_policy: Mapping[str, Sequence[str]] | None,
    regime_affinity_overrides: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, StrategyDeployment]:
    known = {str(name).strip() for name in known_strategies if str(name).strip()}
    tf_policy = {
        str(name).strip(): _normalize_timeframes(value)
        for name, value in (strategy_timeframes_policy or {}).items()
    }
    session_policy = {
        str(name).strip(): _normalize_sessions(value)
        for name, value in (strategy_sessions_policy or {}).items()
    }
    normalized = normalize_strategy_deployments(deployments)

    unknown = sorted(name for name in normalized if name not in known)
    if unknown:
        raise ValueError(
            "strategy_deployment references unregistered strategies: "
            + ", ".join(unknown)
        )

    zero_affinity = sorted(
        name
        for name, mapping in (regime_affinity_overrides or {}).items()
        if _is_zero_affinity_freeze(mapping)
    )
    if zero_affinity:
        raise ValueError(
            "Regime-affinity freeze is no longer allowed; migrate these strategies "
            "to [strategy_deployment.<name>] status instead: "
            + ", ".join(zero_affinity)
        )

    for name, deployment in normalized.items():
        configured_tfs = tf_policy.get(name, tuple())
        configured_sessions = session_policy.get(name, tuple())

        if deployment.locked_timeframes:
            if configured_tfs and set(configured_tfs) != set(
                deployment.locked_timeframes
            ):
                raise ValueError(
                    f"strategy_deployment.{name}.locked_timeframes="
                    f"{list(deployment.locked_timeframes)} does not match "
                    f"strategy_timeframes[{name}]={list(configured_tfs)}"
                )

        if deployment.locked_sessions:
            if configured_sessions and set(configured_sessions) != set(
                deployment.locked_sessions
            ):
                raise ValueError(
                    f"strategy_deployment.{name}.locked_sessions="
                    f"{list(deployment.locked_sessions)} does not match "
                    f"strategy_sessions[{name}]={list(configured_sessions)}"
                )

        if deployment.robustness_tier == "tf_specific":
            _validate_tf_specific_contract(name, deployment)

        if deployment.status is StrategyDeploymentStatus.ACTIVE and (
            deployment.robustness_tier == "tf_specific"
        ):
            raise ValueError(
                f"strategy_deployment.{name}: tf_specific strategy cannot use "
                "status=active; use active_guarded instead"
            )

        if deployment.status is StrategyDeploymentStatus.ACTIVE_GUARDED:
            _validate_guarded_contract(name, deployment)

    return normalized


def _normalize_timeframes(value: Any) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        items = [value]
    cleaned = []
    seen: set[str] = set()
    for item in items:
        tf = str(item).strip().upper()
        if not tf or tf in seen:
            continue
        seen.add(tf)
        cleaned.append(tf)
    return tuple(cleaned)


def _normalize_sessions(value: Any) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        items = [value]
    cleaned = []
    seen: set[str] = set()
    for item in items:
        session = normalize_session_name(str(item).strip())
        if not session or session in seen:
            continue
        seen.add(session)
        cleaned.append(session)
    return tuple(cleaned)


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _maybe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _validate_tf_specific_contract(
    strategy_name: str, deployment: StrategyDeployment
) -> None:
    if not deployment.locked_timeframes:
        raise ValueError(
            f"strategy_deployment.{strategy_name}: tf_specific strategy must set "
            "locked_timeframes"
        )
    if not deployment.locked_sessions:
        raise ValueError(
            f"strategy_deployment.{strategy_name}: tf_specific strategy must set "
            "locked_sessions"
        )
    if deployment.max_live_positions != 1:
        raise ValueError(
            f"strategy_deployment.{strategy_name}: tf_specific strategy must set "
            "max_live_positions=1"
        )
    if not deployment.require_pending_entry:
        raise ValueError(
            f"strategy_deployment.{strategy_name}: tf_specific strategy must set "
            "require_pending_entry=true"
        )
    if not deployment.paper_shadow_required:
        raise ValueError(
            f"strategy_deployment.{strategy_name}: tf_specific strategy must set "
            "paper_shadow_required=true"
        )


def _validate_guarded_contract(
    strategy_name: str, deployment: StrategyDeployment
) -> None:
    if not deployment.locked_timeframes:
        raise ValueError(
            f"strategy_deployment.{strategy_name}: active_guarded strategy must set "
            "locked_timeframes"
        )
    if not deployment.locked_sessions:
        raise ValueError(
            f"strategy_deployment.{strategy_name}: active_guarded strategy must set "
            "locked_sessions"
        )
    if deployment.max_live_positions != 1:
        raise ValueError(
            f"strategy_deployment.{strategy_name}: active_guarded strategy must set "
            "max_live_positions=1"
        )
    if not deployment.require_pending_entry:
        raise ValueError(
            f"strategy_deployment.{strategy_name}: active_guarded strategy must set "
            "require_pending_entry=true"
        )
    if not deployment.paper_shadow_required:
        raise ValueError(
            f"strategy_deployment.{strategy_name}: active_guarded strategy must set "
            "paper_shadow_required=true"
        )


def _is_zero_affinity_freeze(mapping: Mapping[str, float]) -> bool:
    required = ("trending", "ranging", "breakout", "uncertain")
    try:
        return all(float(mapping.get(key, 1.0)) == 0.0 for key in required)
    except (TypeError, ValueError):
        return False
