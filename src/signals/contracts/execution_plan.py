from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .capability import StrategyCapability


def _normalize_capability_row(
    raw: Mapping[str, Any] | StrategyCapability,
) -> dict[str, Any]:
    if isinstance(raw, StrategyCapability):
        raw = raw.as_contract()
    elif not isinstance(raw, Mapping):
        raise TypeError(
            f"Unsupported capability row type: {type(raw).__name__}"
        )

    name = str(raw.get("name") or "").strip()
    if not name:
        return {}

    valid_scopes = tuple(
        dict.fromkeys(
            str(scope).strip()
            for scope in (raw.get("valid_scopes") or ())
            if str(scope).strip()
        )
    )
    needed_indicators = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in (raw.get("needed_indicators") or ())
            if str(item).strip()
        )
    )
    regime_affinity = {
        str(key): float(value)
        for key, value in dict(raw.get("regime_affinity") or {}).items()
        if str(key).strip()
    }
    htf_requirements = {
        str(key): str(value)
        for key, value in dict(raw.get("htf_requirements") or {}).items()
        if str(key).strip() and str(value).strip()
    }

    return {
        "name": name,
        "valid_scopes": list(valid_scopes),
        "needed_indicators": list(needed_indicators),
        "needs_intrabar": bool(raw.get("needs_intrabar")),
        "needs_htf": bool(raw.get("needs_htf")),
        "regime_affinity": regime_affinity,
        "htf_requirements": htf_requirements,
    }


def normalize_capability_contract(
    raw_rows: Iterable[Mapping[str, Any] | StrategyCapability],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        normalized = _normalize_capability_row(raw)
        if normalized:
            rows.append(normalized)
    return tuple(rows)


def _normalize_strategy_list(strategies: Sequence[str]) -> list[str]:
    dedup: dict[str, None] = {}
    for raw in strategies:
        name = str(raw).strip()
        if name:
            dedup[name] = None
    return sorted(dedup.keys())


def _normalize_timeframes_policy(
    policy: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for strategy, timeframes in dict(policy or {}).items():
        strategy_name = str(strategy).strip()
        if not strategy_name:
            continue
        values = sorted(
            {
                str(tf).strip().upper()
                for tf in (timeframes or ())
                if str(tf).strip()
            }
        )
        if values:
            normalized[strategy_name] = values
    return normalized


def build_strategy_capability_summary(
    *,
    capability_contract: Iterable[Mapping[str, Any] | StrategyCapability],
    configured_strategies: Sequence[str],
    scheduled_strategies: Sequence[str],
    strategy_timeframes_policy: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    normalized_rows = normalize_capability_contract(capability_contract)
    capability_by_name = {row["name"]: row for row in normalized_rows if row.get("name")}
    configured = _normalize_strategy_list(configured_strategies)
    scheduled = _normalize_strategy_list(scheduled_strategies)
    scheduled_set = set(scheduled)
    filtered = sorted(set(configured) - scheduled_set)

    required_indicators_by_strategy: dict[str, list[str]] = {}
    required_union: set[str] = set()
    for strategy in scheduled:
        row = capability_by_name.get(strategy)
        if row is None:
            continue
        needed = sorted(
            str(name).strip()
            for name in (row.get("needed_indicators") or [])
            if str(name).strip()
        )
        required_indicators_by_strategy[strategy] = needed
        required_union.update(needed)

    def _scope_strategies(scope: str) -> list[str]:
        result: list[str] = []
        for strategy in scheduled:
            row = capability_by_name.get(strategy)
            if row is None:
                continue
            if scope in (row.get("valid_scopes") or []):
                result.append(strategy)
        return sorted(result)

    active_capability_contract = [
        capability_by_name[strategy]
        for strategy in scheduled
        if strategy in capability_by_name
    ]

    return {
        "strategy_capability_count": len(capability_by_name),
        "configured_strategies": configured,
        "scheduled_strategies": scheduled,
        "filtered_strategies": filtered,
        "scope_strategies": {
            "confirmed": _scope_strategies("confirmed"),
            "intrabar": _scope_strategies("intrabar"),
        },
        "needs_intrabar_strategies": sorted(
            [
                strategy
                for strategy in scheduled
                if bool(capability_by_name.get(strategy, {}).get("needs_intrabar"))
            ]
        ),
        "needs_htf_strategies": sorted(
            [
                strategy
                for strategy in scheduled
                if bool(capability_by_name.get(strategy, {}).get("needs_htf"))
            ]
        ),
        "required_indicators_by_strategy": required_indicators_by_strategy,
        "required_indicators_union": sorted(required_union),
        "strategy_timeframes_policy": _normalize_timeframes_policy(
            strategy_timeframes_policy
        ),
        "active_capability_contract": active_capability_contract,
    }
