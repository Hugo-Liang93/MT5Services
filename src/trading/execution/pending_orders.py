from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalEvent
from src.trading.entry_policy import EntrySpecGroup

from ..broker.comment_codec import (
    build_trade_comment,
    comment_matches_request_id,
    comment_matches_semantics,
    comments_share_request_tag,
)
from ..pending.manager import compute_timeout
from .entry_dispatch import ensure_entry_spec_group
from .eventing import (
    _build_terminal_result,
    build_trade_metadata,
    emit_execution_blocked,
    emit_pending_order_submitted,
    notify_skip,
)
from .reasons import (
    REASON_DUPLICATE_PENDING_SAME_STRATEGY,
    REASON_MISSING_SIGNAL_ID,
    REASON_NEW_SIGNAL_OVERRIDE,
    REASON_ORDER_MISSING_WITHOUT_POSITION,
    REASON_ORDER_ORDERS_LOOKUP_FAILED,
    REASON_ORDER_POSITIONS_LOOKUP_FAILED,
    REASON_PENDING_ORDER_FAILED,
    SKIP_CATEGORY_EXECUTION_INPUT,
    SKIP_CATEGORY_PENDING_SUBMIT,
)
from .sizing import TradeParameters

if TYPE_CHECKING:
    from .executor import TradeExecutor

logger = logging.getLogger(__name__)


def submit_pending_entry(
    executor: "TradeExecutor",
    event: SignalEvent,
    params: TradeParameters,
    cost_metrics: dict[str, float | None],
) -> dict[str, Any | None]:
    if not event.signal_id:
        logger.warning(
            "TradeExecutor: cannot submit pending entry without signal_id for %s/%s",
            event.symbol,
            event.strategy,
        )
        emit_execution_blocked(
            executor,
            event,
            reason=REASON_MISSING_SIGNAL_ID,
            category=SKIP_CATEGORY_EXECUTION_INPUT,
        )
        notify_skip(
            executor, event.signal_id, REASON_MISSING_SIGNAL_ID, event.timeframe or ""
        )
        return _build_terminal_result(
            status="skipped",
            reason=REASON_MISSING_SIGNAL_ID,
            category=SKIP_CATEGORY_EXECUTION_INPUT,
        )

    config = executor.pending_manager.config

    # ADR-013: 从 EntryPolicyRegistry 装配的 EntrySpecGroup 读取入场参数。
    # decision_engine 已在上游 derive 一次写入 metadata，此处幂等取出即可。
    group = ensure_entry_spec_group(executor, event, params)
    if group is None or len(group.members) == 0:
        logger.warning(
            "TradeExecutor: EntrySpecGroup unavailable for %s/%s; skipping pending entry",
            event.symbol,
            event.strategy,
        )
        emit_execution_blocked(
            executor,
            event,
            reason=REASON_PENDING_ORDER_FAILED,
            category=SKIP_CATEGORY_PENDING_SUBMIT,
        )
        notify_skip(
            executor,
            event.signal_id,
            REASON_PENDING_ORDER_FAILED,
            event.timeframe or "",
        )
        return _build_terminal_result(
            status="skipped",
            reason=REASON_PENDING_ORDER_FAILED,
            category=SKIP_CATEGORY_PENDING_SUBMIT,
        )

    timeout = compute_timeout(event.timeframe, config)
    tf = event.timeframe or ""

    # ADR-013 P4: 提交前先撤同 symbol 的旧 group（cancel_by_symbol 已升级为
    # 整组撤逻辑），避免与新 OCO group 冲突。
    if config.cancel_on_new_signal:
        exclude = event.direction if not config.cancel_same_direction else None
        executor.pending_manager.cancel_by_symbol(
            event.symbol,
            reason=REASON_NEW_SIGNAL_OVERRIDE,
            exclude_direction=exclude,
        )

    # ADR-013 P4: 遍历 group.members 全部提交。多 member 时各自独立 ticket，
    # 共享同一 group_id；任一成员 fill 后 _on_member_filled_handler 会撤其他。
    submitted_tickets: list[Any] = []
    last_result: Any = None
    for member in group.members:
        entry_type = member.entry_type.value
        trigger_price = float(member.trigger_price)
        entry_low = float(member.entry_low)
        entry_high = float(member.entry_high)

        # SL/TP 按 trigger_price 与原 params.entry_price 的位移平移，
        # 保持 ATR 单位的 R:R 不变。
        price_shift = trigger_price - params.entry_price
        adjusted_sl = round(params.stop_loss + price_shift, 2)
        adjusted_tp = round(params.take_profit + price_shift, 2)

        order_kind = _resolve_order_kind(event.direction, entry_type)

        # OCO 多 member 时 entry_key 拼接 member_id 避免冲突；单 member 时
        # entry_key=signal_id（向后兼容）。
        entry_key = (
            event.signal_id
            if len(group.members) == 1
            else f"{event.signal_id}#{member.member_id}"
        )

        payload = {
            "symbol": event.symbol,
            "volume": params.position_size,
            "side": event.direction,
            "order_kind": order_kind,
            "price": trigger_price,
            "sl": adjusted_sl,
            "tp": adjusted_tp,
            "comment": build_trade_comment(
                request_id=event.signal_id,
                timeframe=tf,
                strategy=event.strategy,
                side=event.direction,
                order_kind=order_kind,
            ),
            "request_id": entry_key,  # OCO 时各 member 用 entry_key 区分
            "metadata": build_trade_metadata(event),
        }

        try:
            result = executor.trading.dispatch_operation("trade", payload)
            last_result = result
            order_ticket = None
            if isinstance(result, dict):
                order_ticket = result.get("order") or result.get("ticket")

            logger.info(
                "TradeExecutor: placed %s %s %s @ %.2f (zone=[%.2f,%.2f] "
                "type=%s) sl=%.2f tp=%.2f ticket=%s group=%s/%s",
                order_kind,
                event.direction,
                event.symbol,
                trigger_price,
                entry_low,
                entry_high,
                entry_type,
                adjusted_sl,
                adjusted_tp,
                order_ticket,
                group.group_id,
                member.member_id,
            )

            if order_ticket is not None:
                executor.pending_manager.track_mt5_order(
                    signal_id=entry_key,  # 使用 entry_key 作 tracking key
                    order_ticket=order_ticket,
                    expires_at=datetime.now(timezone.utc) + timeout,
                    direction=event.direction,
                    symbol=event.symbol,
                    strategy=event.strategy,
                    timeframe=tf,
                    confidence=event.confidence,
                    regime=event.metadata.get(MK.REGIME),
                    comment=(
                        str(result.get("comment") or payload["comment"])
                        if isinstance(result, dict)
                        else payload["comment"]
                    ),
                    params=params,
                    order_kind=order_kind,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    trigger_price=trigger_price,
                    entry_price_requested=params.entry_price,
                    stop_loss=adjusted_sl,
                    take_profit=adjusted_tp,
                    volume=params.position_size,
                    created_at=datetime.now(timezone.utc),
                    metadata={
                        "entry_type": entry_type,
                        "order_kind": order_kind,
                    },
                    exit_spec=event.metadata.get(MK.EXIT_SPEC),
                    strategy_category=event.metadata.get(MK.STRATEGY_CATEGORY, ""),
                    order_group_id=group.group_id,
                    group_member_id=member.member_id,
                    group_role=member.group_role,
                )
                emit_pending_order_submitted(
                    executor, event, order_kind=order_kind, ticket=order_ticket
                )
                submitted_tickets.append(order_ticket)

            executor.execution_log.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "signal_id": event.signal_id,
                    "entry_key": entry_key,
                    "group_id": group.group_id,
                    "member_id": member.member_id,
                    "symbol": event.symbol,
                    "direction": event.direction,
                    "strategy": event.strategy,
                    "success": True,
                    "pending": True,
                    "order_kind": order_kind,
                    "trigger_price": trigger_price,
                    "order_ticket": order_ticket,
                }
            )
        except Exception as exc:
            logger.error(
                "TradeExecutor: failed to place %s order for %s/%s "
                "(group=%s/%s): %s",
                order_kind,
                event.symbol,
                event.strategy,
                group.group_id,
                member.member_id,
                exc,
            )
            # OCO 多 member 单个失败不阻断其他 member（broker 仍可接受其他单）
            executor.execution_log.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "signal_id": event.signal_id,
                    "entry_key": entry_key,
                    "group_id": group.group_id,
                    "member_id": member.member_id,
                    "symbol": event.symbol,
                    "direction": event.direction,
                    "strategy": event.strategy,
                    "success": False,
                    "pending": True,
                    "error": str(exc),
                }
            )
            continue

    if not submitted_tickets:
        return _build_terminal_result(
            status="failed",
            reason=REASON_PENDING_ORDER_FAILED,
            category=SKIP_CATEGORY_PENDING_SUBMIT,
            details={"reason": "all_oco_members_failed"},
        )

    record_reentry_bar_time(executor, event)
    return last_result if last_result is not None else {"success": True}


def _resolve_order_kind(direction: str, entry_type: str) -> str:
    """从 (direction, entry_type) 推导 MT5 order_kind。

    EntrySpecMember.trigger_price 已由 policy 算好，本函数仅做命名映射：
      buy + limit  → "limit"   (broker 解析为 buy_limit)
      sell + limit → "limit"   (broker 解析为 sell_limit)
      buy + stop   → "stop"
      sell + stop  → "stop"
      *  + market  → "market"
    后续 broker 层会根据 direction 拼成 buy_limit / sell_limit 等具体类型。
    """
    if entry_type in ("limit", "stop", "market"):
        return entry_type
    raise ValueError(f"unknown entry_type {entry_type!r} (direction={direction!r})")


def reached_position_limit(executor: "TradeExecutor", symbol: str) -> bool:
    limit = executor.config.max_concurrent_positions_per_symbol
    if limit is None or limit <= 0:
        return False
    open_positions = open_positions_for_symbol(executor, symbol)
    return open_positions >= limit


def open_positions_for_symbol(executor: "TradeExecutor", symbol: str) -> int:
    tracked_count: int | None = None
    if executor.position_manager is not None:
        try:
            tracked = [
                row
                for row in executor.position_manager.active_positions()
                if row.get("symbol") == symbol
            ]
            tracked_count = len(tracked)
        except (TypeError, AttributeError) as exc:
            logger.debug("Failed to count tracked positions: %s", exc)
            tracked_count = None

    try:
        rows = executor.trading.get_positions(symbol=symbol)
        live_count = len(list(rows or []))
        if tracked_count is None:
            position_count = live_count
        else:
            position_count = max(tracked_count, live_count)
    except Exception:
        position_count = tracked_count or 0

    # 计入尚未成交的挂单，防止多个 pending entry 同时成交后突破持仓限制
    pending_count = _pending_entries_for_symbol(executor, symbol)
    return position_count + pending_count


def open_positions_for_strategy(
    executor: "TradeExecutor",
    *,
    symbol: str,
    strategy: str,
) -> int:
    tracked_count = 0
    if executor.position_manager is not None:
        try:
            tracked = [
                row
                for row in executor.position_manager.active_positions()
                if row.get("symbol") == symbol and row.get("strategy") == strategy
            ]
            tracked_count = len(tracked)
        except (TypeError, AttributeError):
            logger.debug(
                "Failed to count tracked strategy positions for %s/%s",
                symbol,
                strategy,
                exc_info=True,
            )

    live_count = 0
    try:
        rows = executor.trading.get_positions(symbol=symbol)
        live_count = sum(
            1
            for row in list(rows or [])
            if str(row_value(row, "strategy", "") or "").strip() == strategy
        )
    except Exception:
        live_count = 0

    pending_count = 0
    if executor.pending_manager is not None:
        try:
            contexts_fn = getattr(
                executor.pending_manager, "active_execution_contexts", None
            )
            if callable(contexts_fn):
                entries = list(contexts_fn() or [])
            else:
                status = executor.pending_manager.status()
                entries = list(status.get("entries", []) or [])
            pending_count = sum(
                1
                for row in entries
                if row.get("symbol") == symbol and row.get("strategy") == strategy
            )
        except Exception:
            pending_count = 0
            logger.debug(
                "Failed to count pending strategy entries for %s/%s",
                symbol,
                strategy,
                exc_info=True,
            )

    return max(tracked_count, live_count) + pending_count


def _pending_entries_for_symbol(executor: "TradeExecutor", symbol: str) -> int:
    """统计指定品种的活跃挂单数量。"""
    if executor.pending_manager is None:
        return 0
    try:
        contexts_fn = getattr(
            executor.pending_manager, "active_execution_contexts", None
        )
        if callable(contexts_fn):
            entries = list(contexts_fn() or [])
        else:
            status = executor.pending_manager.status()
            entries = list(status.get("entries", []) or [])
        return sum(1 for e in entries if e.get("symbol") == symbol)
    except Exception:
        logger.debug("Failed to count pending entries for %s", symbol, exc_info=True)
        return 0


def duplicate_execution_reason(executor: "TradeExecutor", event: SignalEvent) -> str:
    """重复入场拦截判定（按优先级）。

    layer-0 (历史 has_matching_active_position 同 (symbol, tf, strategy)
    任意方向拦) 已删除——实测发现该约束让系统在浮亏老仓 + 反向新信号场
    景下被锁死（同 TF 反向不能开仓，浮亏不主动平仓，等死 SL）。

    现行栈靠多层独立 cap：
    - reentry_cooldown_bars: 限同 (symbol, tf, strategy, direction) 重复频率
    - max_positions_per_symbol / max_volume_per_symbol: 总笔数 / 仓位上限
    - daily_loss_limit_pct: 日损上限
    - single_trade_loss_cap: 单笔损失上限

    duplicate_execution_reason 现在只剩：
    - 同策略 pending entry 已存在 → REASON_DUPLICATE_PENDING_SAME_STRATEGY
    """
    if has_matching_pending_entry(executor, event):
        return REASON_DUPLICATE_PENDING_SAME_STRATEGY
    return ""


def has_matching_pending_entry(executor: "TradeExecutor", event: SignalEvent) -> bool:
    """Duplicate guard：(symbol, timeframe, strategy) 已有 pending 入场则拦下新信号。

    与 has_matching_active_position 对称——pending 入场也属于该策略 TF
    "尚未关闭" 的产出，反向新信号同样拒。
    """
    if executor.pending_manager is None:
        return False
    try:
        contexts_fn = getattr(
            executor.pending_manager, "active_execution_contexts", None
        )
        if callable(contexts_fn):
            entries = list(contexts_fn() or [])
        else:
            status = executor.pending_manager.status()
            entries = list(status.get("entries", []) or [])
    except Exception:
        logger.debug(
            "Failed to inspect pending entries for duplicate guard",
            exc_info=True,
        )
        return False
    for row in entries:
        if (
            row.get("symbol") == event.symbol
            and row.get("timeframe") == event.timeframe
            and row.get("strategy") == event.strategy
        ):
            return True
    return False


def record_reentry_bar_time(executor: "TradeExecutor", event: SignalEvent) -> None:
    bar_time_raw = event.metadata.get(MK.BAR_TIME)
    bar_time_value: datetime | None = None
    if isinstance(bar_time_raw, datetime):
        bar_time_value = bar_time_raw
    elif isinstance(bar_time_raw, str):
        try:
            bar_time_value = datetime.fromisoformat(bar_time_raw.replace("Z", "+00:00"))
        except ValueError:
            bar_time_value = None
    if bar_time_value is None:
        return
    executor.last_entry_bar_time[
        (event.symbol, event.timeframe, event.strategy, event.direction)
    ] = bar_time_value


def row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def position_direction(row: Any, default_direction: str = "") -> str:
    direction = str(row_value(row, "action", "") or "").strip().lower()
    if direction in {"buy", "sell"}:
        return direction
    try:
        position_type = int(row_value(row, "type", 0) or 0)
        return "sell" if position_type == 1 else "buy"
    except (TypeError, ValueError):
        return str(default_direction or "").strip().lower()


def find_live_position_for_pending_order(
    positions: list[Any],
    *,
    symbol: str,
    direction: str,
    comment: str,
    signal_id: str = "",
    timeframe: str = "",
    strategy: str = "",
) -> Any | None:
    target_symbol = str(symbol or "").strip()
    target_direction = str(direction or "").strip().lower()
    target_comment = str(comment or "").strip()
    exact_matches: list[Any] = []
    request_tag_matches: list[Any] = []
    semantic_matches: list[Any] = []
    directional_matches: list[Any] = []
    for row in positions or []:
        row_symbol = str(row_value(row, "symbol", "") or "").strip()
        if target_symbol and row_symbol != target_symbol:
            continue
        row_direction = position_direction(row, default_direction=target_direction)
        if target_direction and row_direction and row_direction != target_direction:
            continue
        row_comment = str(row_value(row, "comment", "") or "").strip()
        if target_comment and row_comment == target_comment:
            exact_matches.append(row)
            continue
        if (
            target_comment and comments_share_request_tag(row_comment, target_comment)
        ) or (signal_id and comment_matches_request_id(row_comment, signal_id)):
            request_tag_matches.append(row)
            continue
        if comment_matches_semantics(row_comment, timeframe, strategy):
            semantic_matches.append(row)
            continue
        directional_matches.append(row)
    matches = exact_matches or request_tag_matches or semantic_matches
    if not matches and len(directional_matches) == 1:
        matches = directional_matches
    if not matches:
        return None
    matches.sort(
        key=lambda row: (
            int(row_value(row, "time_msc", 0) or 0),
            (
                int(raw_time.timestamp())
                if isinstance((raw_time := row_value(row, "time", 0)), datetime)
                else int(raw_time or 0)
            ),
            int(row_value(row, "ticket", 0) or 0),
        )
    )
    return matches[-1]


def tracked_position_tickets(executor: "TradeExecutor") -> set[int]:
    if executor.position_manager is None:
        return set()
    try:
        active_positions = executor.position_manager.active_positions()
    except Exception as exc:
        logger.debug("Failed to inspect tracked position tickets: %s", exc)
        return set()
    tickets: set[int] = set()
    for row in active_positions or []:
        try:
            ticket = int(row_value(row, "ticket", 0) or 0)
        except (TypeError, ValueError):
            continue
        if ticket > 0:
            tickets.add(ticket)
    return tickets


def params_from_pending_fill(
    base_params: TradeParameters | None,
    *,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    volume: float,
) -> TradeParameters:
    if isinstance(base_params, TradeParameters):
        resolved_sl = stop_loss if stop_loss > 0 else base_params.stop_loss
        resolved_tp = take_profit if take_profit > 0 else base_params.take_profit
        resolved_volume = volume if volume > 0 else base_params.position_size
        return replace(
            base_params,
            entry_price=entry_price,
            stop_loss=resolved_sl,
            take_profit=resolved_tp,
            position_size=resolved_volume,
            sl_distance=abs(entry_price - resolved_sl),
            tp_distance=abs(resolved_tp - entry_price),
        )

    resolved_sl = stop_loss if stop_loss > 0 else entry_price
    resolved_tp = take_profit if take_profit > 0 else entry_price
    sl_distance = abs(entry_price - resolved_sl)
    tp_distance = abs(resolved_tp - entry_price)
    rr = (tp_distance / sl_distance) if sl_distance > 0 else 0.0
    return TradeParameters(
        entry_price=entry_price,
        stop_loss=resolved_sl,
        take_profit=resolved_tp,
        position_size=volume,
        risk_reward_ratio=rr,
        atr_value=sl_distance / 2.0 if sl_distance > 0 else 0.0,
        sl_distance=sl_distance,
        tp_distance=tp_distance,
    )


def inspect_pending_mt5_order(
    executor: "TradeExecutor", info: dict[str, Any]
) -> dict[str, Any]:
    order_ticket = int(info.get("ticket", 0) or 0)
    symbol = str(info.get("symbol") or "").strip()
    direction = str(info.get("direction") or "").strip().lower()
    comment = str(info.get("comment") or "").strip()
    signal_id = str(info.get("signal_id") or "").strip()

    try:
        open_orders = list(executor.trading.get_orders(symbol=symbol))
    except Exception as exc:
        logger.debug(
            "TradeExecutor: get_orders(%s) failed while inspecting pending MT5 order %s: %s",
            symbol,
            order_ticket,
            exc,
        )
        return {"status": "pending", "reason": REASON_ORDER_ORDERS_LOOKUP_FAILED}
    for row in open_orders or []:
        try:
            live_order_ticket = int(row_value(row, "ticket", 0) or 0)
        except (TypeError, ValueError):
            continue
        if live_order_ticket == order_ticket:
            return {"status": "pending"}

    try:
        open_positions = list(executor.trading.get_positions(symbol=symbol))
    except Exception as exc:
        logger.debug(
            "TradeExecutor: get_positions(%s) failed while inspecting pending MT5 order %s: %s",
            symbol,
            order_ticket,
            exc,
        )
        return {"status": "pending", "reason": REASON_ORDER_POSITIONS_LOOKUP_FAILED}

    raw_position = find_live_position_for_pending_order(
        open_positions,
        symbol=symbol,
        direction=direction,
        comment=comment,
        signal_id=signal_id,
        timeframe=str(info.get("timeframe") or ""),
        strategy=str(info.get("strategy") or ""),
    )
    if raw_position is None:
        return {"status": "missing", "reason": REASON_ORDER_MISSING_WITHOUT_POSITION}

    position_ticket = int(row_value(raw_position, "ticket", 0) or 0)
    fill_price = float(row_value(raw_position, "price_open", 0.0) or 0.0)
    stop_loss = float(row_value(raw_position, "sl", 0.0) or 0.0)
    take_profit = float(row_value(raw_position, "tp", 0.0) or 0.0)
    volume = float(row_value(raw_position, "volume", 0.0) or 0.0)
    opened_at = row_value(raw_position, "time", None)
    resolved_comment = str(row_value(raw_position, "comment", "") or comment)
    params = params_from_pending_fill(
        info.get("params"),
        entry_price=fill_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        volume=volume,
    )

    if (
        executor.position_manager is not None
        and position_ticket > 0
        and position_ticket not in tracked_position_tickets(executor)
    ):
        try:
            executor.position_manager.track_position(
                ticket=position_ticket,
                signal_id=signal_id or f"restored:{position_ticket}",
                symbol=symbol,
                action=direction or position_direction(raw_position),
                params=params,
                timeframe=str(info.get("timeframe") or ""),
                strategy=str(info.get("strategy") or ""),
                confidence=info.get("confidence"),
                regime=info.get("regime"),
                comment=resolved_comment,
                opened_at=opened_at,
                fill_price=fill_price if fill_price > 0 else None,
                exit_spec=info.get("exit_spec"),
                strategy_category=str(info.get("strategy_category") or ""),
            )
        except Exception as exc:
            logger.warning(
                "TradeExecutor: failed to register filled pending MT5 order ticket=%s -> position=%s: %s",
                order_ticket,
                position_ticket,
                exc,
            )

    if executor.trade_outcome_tracker is not None and signal_id:
        try:
            # §0dk P2：TradeExecutor.runtime_identity 必填（§0dj），直接 access。
            runtime_identity = executor.runtime_identity
            metadata = dict(info.get("metadata") or {})
            executor.trade_outcome_tracker.on_trade_opened(
                signal_id=signal_id,
                symbol=symbol,
                timeframe=str(info.get("timeframe") or ""),
                strategy=str(info.get("strategy") or ""),
                direction=direction or position_direction(raw_position),
                fill_price=fill_price if fill_price > 0 else params.entry_price,
                confidence=float(info.get("confidence") or 0.0),
                regime=info.get("regime"),
                account_key=metadata.get("target_account_key")
                or runtime_identity.account_key,
                account_alias=metadata.get("target_account_alias")
                or runtime_identity.account_alias,
                intent_id=metadata.get("intent_id"),
                opened_at=opened_at,
            )
        except Exception as exc:
            logger.warning(
                "TradeExecutor: failed to restore trade outcome tracking for signal=%s pending MT5 fill: %s",
                signal_id,
                exc,
            )

    return {
        "status": "filled",
        "ticket": position_ticket,
        "fill_price": fill_price,
    }
