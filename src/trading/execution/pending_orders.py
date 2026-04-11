from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalEvent

from ..pending.manager import compute_timeout
from .eventing import (
    build_trade_metadata,
    emit_execution_blocked,
    emit_execution_failed,
    emit_pending_order_submitted,
    notify_skip,
)
from .sizing import TradeParameters
from .reasons import (
    REASON_MISSING_SIGNAL_ID,
    REASON_NEW_SIGNAL_OVERRIDE,
    REASON_ORDER_ORDERS_LOOKUP_FAILED,
    REASON_ORDER_POSITIONS_LOOKUP_FAILED,
    REASON_DUPLICATE_POSITION_SAME_STRATEGY,
    REASON_DUPLICATE_PENDING_SAME_STRATEGY,
    REASON_PENDING_ORDER_FAILED,
    REASON_ORDER_MISSING_WITHOUT_POSITION,
)
from .reasons import (
    SKIP_CATEGORY_PENDING_SUBMIT,
    SKIP_CATEGORY_EXECUTION_INPUT,
)

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
        return None

    config = executor.pending_manager.config

    # 从策略 entry_spec 读取入场意图
    entry_spec = event.metadata.get(MK.ENTRY_SPEC, {})
    entry_type = entry_spec.get("entry_type", "market")
    suggested_price = entry_spec.get("entry_price")
    zone_atr = entry_spec.get("entry_zone_atr", 0.3)

    ref_price = (
        float(suggested_price) if suggested_price is not None else params.entry_price
    )
    half_zone = zone_atr * params.atr_value
    entry_low = ref_price - half_zone
    entry_high = ref_price + half_zone

    timeout = compute_timeout(event.timeframe, config)

    order_kind, trigger_price = resolve_pending_order(
        direction=event.direction,
        entry_type=entry_type,
        entry_low=entry_low,
        entry_high=entry_high,
    )

    price_shift = trigger_price - params.entry_price
    adjusted_sl = round(params.stop_loss + price_shift, 2)
    adjusted_tp = round(params.take_profit + price_shift, 2)

    if config.cancel_on_new_signal:
        exclude = event.direction if not config.cancel_same_direction else None
        executor.pending_manager.cancel_by_symbol(
            event.symbol,
            reason=REASON_NEW_SIGNAL_OVERRIDE,
            exclude_direction=exclude,
        )

    tf = event.timeframe or ""
    payload = {
        "symbol": event.symbol,
        "volume": params.position_size,
        "side": event.direction,
        "order_kind": order_kind,
        "price": trigger_price,
        "sl": adjusted_sl,
        "tp": adjusted_tp,
        "comment": f"{tf}:{event.strategy}:{order_kind}"[:31],
        "request_id": event.signal_id,
        "metadata": build_trade_metadata(event),
    }

    try:
        result = executor.trading.dispatch_operation("trade", payload)
        order_ticket = None
        if isinstance(result, dict):
            order_ticket = result.get("order") or result.get("ticket")

        logger.info(
            "TradeExecutor: placed %s %s %s @ %.2f (zone=[%.2f,%.2f] type=%s) sl=%.2f tp=%.2f ticket=%s",
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
        )

        if order_ticket is not None:
            executor.pending_manager.track_mt5_order(
                signal_id=event.signal_id,
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
            )
            emit_pending_order_submitted(
                executor, event, order_kind=order_kind, ticket=order_ticket
            )

        executor.execution_log.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
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
        record_reentry_bar_time(executor, event)
        return result

    except Exception as exc:
        logger.error(
            "TradeExecutor: failed to place %s order for %s/%s: %s",
            order_kind,
            event.symbol,
            event.strategy,
            exc,
        )
        emit_execution_failed(
            executor,
            event,
            order_kind=order_kind,
            reason=str(exc),
            category=SKIP_CATEGORY_PENDING_SUBMIT,
        )
        notify_skip(
            executor, event.signal_id, f"{REASON_PENDING_ORDER_FAILED}:{exc}", tf
        )
        return None


def resolve_pending_order(
    direction: str,
    entry_type: str,
    entry_low: float,
    entry_high: float,
) -> tuple[str, float]:
    """根据策略指定的 entry_type 决定订单类型和触发价。"""
    if entry_type == "stop":
        # stop order：buy 追高 / sell 追低
        if direction == "buy":
            return "stop", entry_high
        return "stop", entry_low
    # limit order（默认）：buy 等回调 / sell 等反弹
    if direction == "buy":
        return "limit", entry_low
    return "limit", entry_high


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
    if has_matching_active_position(executor, event):
        return REASON_DUPLICATE_POSITION_SAME_STRATEGY
    if has_matching_pending_entry(executor, event):
        return REASON_DUPLICATE_PENDING_SAME_STRATEGY
    return ""


def has_matching_active_position(executor: "TradeExecutor", event: SignalEvent) -> bool:
    if executor.position_manager is None:
        return False
    try:
        active_positions = executor.position_manager.active_positions()
    except Exception:
        logger.debug(
            "Failed to inspect active positions for duplicate guard",
            exc_info=True,
        )
        return False
    for row in active_positions or []:
        if (
            row.get("symbol") == event.symbol
            and row.get("timeframe") == event.timeframe
            and row.get("strategy") == event.strategy
            and row.get("action") == event.direction
        ):
            return True
    return False


def has_matching_pending_entry(executor: "TradeExecutor", event: SignalEvent) -> bool:
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
            and row.get("direction") == event.direction
        ):
            return True
    return False


def record_reentry_bar_time(executor: "TradeExecutor", event: SignalEvent) -> None:
    bar_time = event.metadata.get(MK.BAR_TIME)
    if bar_time is None:
        return
    executor.last_entry_bar_time[(event.symbol, event.strategy, event.direction)] = (
        bar_time
    )


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
    timeframe: str = "",
    strategy: str = "",
) -> Any | None:
    target_symbol = str(symbol or "").strip()
    target_direction = str(direction or "").strip().lower()
    target_comment = str(comment or "").strip()
    target_prefix = comment_prefix(timeframe, strategy)
    exact_matches: list[Any] = []
    prefix_matches: list[Any] = []
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
        if target_prefix and row_comment.lower().startswith(target_prefix):
            prefix_matches.append(row)
            continue
        directional_matches.append(row)
    matches = exact_matches or prefix_matches
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


def comment_prefix(timeframe: str, strategy: str) -> str:
    tf = str(timeframe or "").strip().lower()
    strat = str(strategy or "").strip().lower()
    if not tf or not strat:
        return ""
    return f"{tf}:{strat}:"


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
            executor.trade_outcome_tracker.on_trade_opened(
                signal_id=signal_id,
                symbol=symbol,
                timeframe=str(info.get("timeframe") or ""),
                strategy=str(info.get("strategy") or ""),
                direction=direction or position_direction(raw_position),
                fill_price=fill_price if fill_price > 0 else params.entry_price,
                confidence=float(info.get("confidence") or 0.0),
                regime=info.get("regime"),
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
