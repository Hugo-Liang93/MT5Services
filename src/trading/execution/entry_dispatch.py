"""ADR-013 入场分发集中点。

从 SignalEvent.metadata[ENTRY_INTENT] + ATR + RECENT_BARS 构造 EntryIntent +
MarketSnapshot，调 EntryPolicyRegistry.resolve(strategy, tf).derive(...) 拿
EntrySpecGroup 写入 event.metadata[ENTRY_SPEC_GROUP]。

下游 decision_engine / pre_trade_checks / submit_pending_entry / readmodel 一律
读 event.metadata[ENTRY_SPEC_GROUP]，不再各自 derive。

ensure_entry_spec_group() 是幂等的：已写入则直接返回，未写入才 derive。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping

from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalEvent
from src.trading.entry_policy import (
    BarSnapshot,
    EntryIntent,
    EntryPolicyMappingError,
    EntryPolicyNotFoundError,
    EntryPolicyRegistry,
    EntrySpecGroup,
    MarketSnapshot,
    PatternType,
)

if TYPE_CHECKING:
    from src.trading.execution.executor import TradeExecutor
    from src.trading.execution.sizing import TradeParameters

logger = logging.getLogger(__name__)


class EntryDispatchError(RuntimeError):
    """ENTRY_SPEC_GROUP 装配失败：缺乏 registry / 缺乏 ATR / 缺乏 ENTRY_INTENT。"""


def ensure_entry_spec_group(
    executor: "TradeExecutor",
    event: SignalEvent,
    params: "TradeParameters",
) -> EntrySpecGroup | None:
    """幂等地将 EntrySpecGroup 装配到 event.metadata[ENTRY_SPEC_GROUP]。

    返回 None 表示装配失败（registry 缺失 / ATR<=0 / 关键字段缺失）。
    调用方必须结构化拒单；不得把 None 解释为 market 入场。
    """
    cached = event.metadata.get(MK.ENTRY_SPEC_GROUP)
    if isinstance(cached, EntrySpecGroup):
        return cached

    registry = _resolve_registry(executor)
    if registry is None:
        return None

    intent = _build_entry_intent(event)
    if intent is None:
        return None

    market = _build_market_snapshot(event, params)
    if market is None:
        return None

    try:
        policy = registry.resolve(intent.strategy_name, intent.timeframe)
    except (EntryPolicyMappingError, EntryPolicyNotFoundError) as exc:
        logger.warning(
            "EntryPolicy mapping unavailable for %s/%s: %s",
            intent.strategy_name,
            intent.timeframe,
            exc,
        )
        return None
    policy_params = registry.resolve_params(policy.name, intent.timeframe)
    group = policy.derive(intent, market, policy_params)

    event.metadata[MK.ENTRY_SPEC_GROUP] = group
    event.metadata[MK.ENTRY_POLICY_DECISION] = group.to_dict()

    # ADR-013: 审计落库（best-effort，失败不阻断交易）
    _record_decision(executor, event, intent, policy, group)

    return group


def _record_decision(
    executor: "TradeExecutor",
    event: SignalEvent,
    intent: EntryIntent,
    policy: Any,
    group: EntrySpecGroup,
) -> None:
    """把 EntryPolicy decision 写入 entry_policy_decisions 表。失败仅警告。"""
    try:
        store = _resolve_decision_store(executor)
        if store is None:
            return
        account_key = _resolve_account_key(executor)
        if not account_key:
            return
        signal_id = event.signal_id
        if not signal_id:
            return
        branch = group.metadata.get("branch") if group.metadata else None
        row = (
            account_key,
            signal_id,
            intent.strategy_name,
            intent.timeframe,
            intent.direction,
            intent.pattern_type.value,
            policy.name,
            str(branch) if branch is not None else None,
            group.group_id,
            group.cancellation_policy,
            [m for m in group.to_dict()["members"]],
            intent.bar_time,
            None,  # fill_member_id（fill 时再 update）
            None,  # fill_at
            None,  # fill_price
            None,  # fill_outcome
            dict(group.metadata) if group.metadata else {},
            datetime.now(timezone.utc),
        )
        store.write_decisions([row])
    except Exception as exc:  # ADR-013 审计 best-effort: 失败不能阻断交易
        logger.warning("EntryPolicy decision audit failed: %s", exc)


def _resolve_decision_store(executor: "TradeExecutor") -> Any:
    """通过 executor 拿审计表 repo（trading_module / persist_execution_fn 链路）。"""
    persist_fn = getattr(executor, "persist_execution_fn", None)
    if persist_fn is None:
        return None
    # persist_fn 一般是 storage_writer.db.write_auto_executions 之类的 bound method；
    # 取其所属 db 对象上的 entry_policy_repo。
    self_obj = getattr(persist_fn, "__self__", None)
    if self_obj is None:
        return None
    repo = getattr(self_obj, "entry_policy_repo", None)
    return repo


def _resolve_account_key(executor: "TradeExecutor") -> str:
    runtime_identity = getattr(executor, "runtime_identity", None)
    if runtime_identity is None:
        return ""
    account_key = getattr(runtime_identity, "account_key", None)
    return str(account_key) if account_key else ""


def _resolve_registry(executor: "TradeExecutor") -> EntryPolicyRegistry | None:
    pending = getattr(executor, "pending_manager", None)
    if pending is None:
        return None
    registry = getattr(pending, "entry_policy_registry", None)
    if isinstance(registry, EntryPolicyRegistry):
        return registry
    return None


def _build_entry_intent(event: SignalEvent) -> EntryIntent | None:
    raw = event.metadata.get(MK.ENTRY_INTENT)
    if not isinstance(raw, Mapping):
        return None
    pattern_value = raw.get("pattern_type") or PatternType.NONE.value
    try:
        pattern = PatternType(pattern_value)
    except ValueError:
        pattern = PatternType.NONE

    direction = raw.get("direction") or event.direction
    if direction not in ("buy", "sell"):
        return None

    bar_time_raw = event.metadata.get(MK.BAR_TIME) or event.generated_at
    if isinstance(bar_time_raw, datetime):
        bar_time = bar_time_raw
    else:
        bar_time = datetime.now(timezone.utc)

    return EntryIntent(
        strategy_name=str(raw.get("strategy_name") or event.strategy),
        timeframe=str(raw.get("timeframe") or event.timeframe),
        direction=direction,  # type: ignore[arg-type]
        confidence=float(event.confidence),
        bar_time=bar_time,
        pattern_type=pattern,
        signal_metadata=raw,
    )


def _build_market_snapshot(
    event: SignalEvent,
    params: "TradeParameters",
) -> MarketSnapshot | None:
    atr_value = float(getattr(params, "atr_value", 0.0) or 0.0)
    if atr_value <= 0:
        return None

    raw_bars = event.metadata.get(MK.RECENT_BARS)
    if not isinstance(raw_bars, (list, tuple)) or not raw_bars:
        return None

    bars: list[BarSnapshot] = []
    for raw in raw_bars:
        if isinstance(raw, BarSnapshot):
            bars.append(raw)
        elif isinstance(raw, Mapping):
            try:
                bars.append(BarSnapshot.from_mapping(raw))
            except (KeyError, TypeError, ValueError):
                continue
    if not bars:
        return None

    current_close = float(event.metadata.get(MK.CLOSE_PRICE) or bars[-1].close)
    market_structure = event.metadata.get(MK.MARKET_STRUCTURE) or {}
    atr_source = str(event.metadata.get(MK.INTRABAR_ATR_SOURCE) or "confirmed")

    return MarketSnapshot(
        recent_bars=tuple(bars),
        atr_value=atr_value,
        current_close=current_close,
        current_atr_source=atr_source,
        market_structure=(
            market_structure if isinstance(market_structure, Mapping) else {}
        ),
    )


def group_is_market_only(group: EntrySpecGroup | None) -> bool:
    """判断 EntrySpecGroup 是否只含 MARKET 成员；None 不是 market。"""
    if group is None:
        return False
    return group.all_market()


__all__ = [
    "EntryDispatchError",
    "ensure_entry_spec_group",
    "group_is_market_only",
]
