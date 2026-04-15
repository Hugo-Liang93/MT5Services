"""持仓对账与恢复逻辑（从 PositionManager 提取的纯函数模块）。

所有函数接收 manager 引用作为显式参数（ADR-002 模式）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..trade_events import (
    POSITION_CLOSE_SOURCE_HISTORY_DEALS,
    POSITION_CLOSE_SOURCE_MT5_MISSING,
    POSITION_PARTIALLY_CLOSED,
    POSITION_RECOVERED,
)

if TYPE_CHECKING:
    from .manager import PositionManager, TrackedPosition

logger = logging.getLogger(__name__)


def force_close_overnight(manager: PositionManager) -> Optional[Dict[str, Any]]:
    """启动时检测并强制平仓过夜仓位。

    如果 EOD 因服务宕机而被跳过，次日启动时立即全平。
    只在 end_of_day_close_enabled=True 时生效。
    """
    if not manager.end_of_day_close_enabled:
        return None

    try:
        open_positions = list(manager._trading.get_positions())
    except Exception:
        return None
    if not open_positions:
        return None

    now = datetime.now(timezone.utc)
    overnight: list = []
    for pos in open_positions:
        opened = getattr(pos, "time", None)
        if opened is None:
            overnight.append(pos)
            continue
        if not isinstance(opened, datetime):
            overnight.append(pos)
            continue
        opened_utc = (
            opened if opened.tzinfo else opened.replace(tzinfo=timezone.utc)
        )
        if opened_utc.date() < now.date():
            overnight.append(pos)

    if not overnight:
        return None

    logger.warning(
        "PositionManager: detected %d overnight positions, force closing",
        len(overnight),
    )
    try:
        result = manager._trading.close_all_positions(
            comment="overnight_force_close"
        )
        logger.info("Overnight force close result: %s", result)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as exc:
        logger.error("Overnight force close failed: %s", exc)
        return {"error": str(exc)}


def sync_open_positions(manager: PositionManager) -> dict[str, Any]:
    """从 MT5 恢复已有持仓到 PositionManager 的跟踪列表。"""
    from .manager import TrackedPosition

    try:
        open_positions = list(manager._trading.get_positions())
    except Exception as exc:
        manager._last_error = f"sync_open_positions: {exc}"
        logger.warning("PositionManager sync_open_positions error: %s", exc)
        return {"synced": 0, "recovered": 0, "skipped": 0, "error": str(exc)}

    synced = 0
    recovered = 0
    skipped = 0
    for raw_pos in open_positions:
        ticket = int(getattr(raw_pos, "ticket", 0) or 0)
        if ticket <= 0:
            skipped += 1
            continue
        with manager._lock:
            if ticket in manager._positions:
                skipped += 1
                continue

        comment = str(getattr(raw_pos, "comment", "") or "")
        persisted_state = None
        if manager._position_state_resolver is not None:
            try:
                persisted_state = dict(
                    manager._position_state_resolver(ticket) or {}
                )
            except Exception:
                logger.debug(
                    "Position state resolver failed for ticket=%s",
                    ticket,
                    exc_info=True,
                )
                persisted_state = None
        context = None
        if manager._position_context_resolver is not None:
            try:
                context = manager._position_context_resolver(ticket, comment)
            except Exception:
                logger.debug(
                    "Position context resolver failed for ticket=%s",
                    ticket,
                    exc_info=True,
                )
                context = None
        merged_context = dict(persisted_state or {})
        merged_context.update(dict(context or {}))
        effective_comment = str(merged_context.get("comment") or comment)
        if not merged_context.get(
            "signal_id"
        ) and not manager._is_restorable_comment(effective_comment):
            skipped += 1
            continue

        action = str(
            merged_context.get("action")
            or manager._action_from_position_type(
                getattr(raw_pos, "type", None)
            )
        )
        entry_price = float(
            merged_context.get("entry_price")
            or getattr(raw_pos, "price_open", 0.0)
            or 0.0
        )
        stop_loss = float(getattr(raw_pos, "sl", 0.0) or 0.0)
        take_profit = float(getattr(raw_pos, "tp", 0.0) or 0.0)
        volume = float(getattr(raw_pos, "volume", 0.0) or 0.0)
        opened_at = getattr(raw_pos, "time", None)
        if not isinstance(opened_at, datetime):
            opened_at = datetime.now(timezone.utc)

        symbol_str = str(getattr(raw_pos, "symbol", "") or "")
        timeframe_str = str(merged_context.get("timeframe") or "")

        pos = TrackedPosition(
            ticket=ticket,
            signal_id=str(
                merged_context.get("signal_id") or f"restored:{ticket}"
            ),
            symbol=symbol_str,
            action=action,
            entry_price=float(
                merged_context.get("fill_price") or entry_price
            ),
            stop_loss=stop_loss,
            take_profit=take_profit,
            volume=volume,
            atr_at_entry=float(
                merged_context.get("atr_at_entry")
                or manager._default_atr_from_position(entry_price, stop_loss)
            ),
            timeframe=timeframe_str,
            strategy=str(merged_context.get("strategy") or ""),
            confidence=merged_context.get("confidence"),
            regime=merged_context.get("regime"),
            source=str(merged_context.get("source") or "mt5_bootstrap"),
            comment=effective_comment,
            opened_at=opened_at,
            highest_price=(
                merged_context.get("highest_price")
                if merged_context.get("highest_price") is not None
                else (entry_price if action == "buy" else None)
            ),
            lowest_price=(
                merged_context.get("lowest_price")
                if merged_context.get("lowest_price") is not None
                else (entry_price if action == "sell" else None)
            ),
            current_price=merged_context.get("current_price"),
            breakeven_applied=bool(
                merged_context.get("breakeven_applied")
                or (
                    (action == "buy" and stop_loss >= entry_price)
                    or (
                        action == "sell"
                        and stop_loss <= entry_price
                        and stop_loss > 0
                    )
                )
            ),
            trailing_active=bool(merged_context.get("trailing_active")),
        )
        # 关键恢复一致性：breakeven_applied（DB 持久化标志）必须同步到
        # breakeven_activated（运行时活跃标志），否则 _evaluate_chandelier_exit
        # 会以为 breakeven 还没激活，可能重复触发 breakeven 移动逻辑。
        # SL 已在 entry 之上时虽然 max(new_sl, current_sl) 不会真的回退，
        # 但状态语义错乱会让后续判断（如 lock_ratio 计算）基于错误的"未激活"前提。
        if pos.breakeven_applied:
            pos.breakeven_activated = True
        # Chandelier Exit 基线恢复：
        # 优先使用 DB 持久化的 initial_stop_loss（开仓时记录，不随 trailing 漂移），
        # 只有在从未持久化过的历史持仓（如首次启动扫描到 broker-side 遗留单）时
        # 才 fallback 到当前 SL 作为近似 baseline。
        persisted_initial_sl = None
        raw_initial_sl = merged_context.get("initial_stop_loss")
        if raw_initial_sl is not None:
            try:
                coerced = float(raw_initial_sl)
            except (TypeError, ValueError):
                coerced = 0.0
            if coerced > 0:
                persisted_initial_sl = coerced
        effective_initial_sl = (
            persisted_initial_sl if persisted_initial_sl is not None else stop_loss
        )
        if effective_initial_sl > 0:
            pos.initial_stop_loss = float(effective_initial_sl)
            pos.initial_risk = abs(pos.entry_price - pos.initial_stop_loss)
        if pos.atr_at_entry > 0 and pos.initial_risk > 0:
            pos.sl_atr_mult = round(pos.initial_risk / pos.atr_at_entry, 4)
        elif pos.atr_at_entry > 0 and pos.stop_loss > 0:
            pos.sl_atr_mult = round(
                abs(pos.entry_price - pos.stop_loss) / pos.atr_at_entry,
                4,
            )
        with manager._lock:
            manager._positions[ticket] = pos
        if manager._on_position_tracked is not None:
            manager._on_position_tracked(pos, POSITION_RECOVERED)
        synced += 1
        if (
            manager._recovered_position_callback is not None
            and merged_context.get("signal_id")
        ):
            try:
                manager._recovered_position_callback(pos)
                recovered += 1
            except Exception:
                logger.warning(
                    "Recovered position callback failed for ticket=%s",
                    ticket,
                    exc_info=True,
                )
    return {"synced": synced, "recovered": recovered, "skipped": skipped}


def reconcile_with_mt5(manager: PositionManager) -> None:
    """与 MT5 终端同步持仓状态。"""
    # 每个对账周期先尝试恢复新开仓位
    try:
        recovery = sync_open_positions(manager)
        if int(recovery.get("synced", 0) or 0) > 0:
            logger.info(
                "PositionManager reconcile recovered positions: %s", recovery
            )
    except Exception as exc:
        logger.debug(
            "PositionManager: sync_open_positions during reconcile failed: %s",
            exc,
        )

    with manager._lock:
        tracked_tickets = dict(manager._positions)

    if not tracked_tickets:
        return

    symbols: set[str] = {pos.symbol for pos in tracked_tickets.values()}
    mt5_positions: Dict[int, Any] = {}
    failed_symbols: set[str] = set()
    for symbol in symbols:
        try:
            open_positions = manager._trading.get_positions(symbol=symbol)
            for raw_pos in open_positions or []:
                ticket = getattr(raw_pos, "ticket", None)
                if ticket is not None:
                    mt5_positions[int(ticket)] = raw_pos
        except Exception as exc:
            failed_symbols.add(symbol)
            logger.debug(
                "PositionManager: get_positions(%s) error: %s", symbol, exc
            )

    for ticket, pos in tracked_tickets.items():
        if pos.symbol in failed_symbols:
            continue
        mt5_pos = mt5_positions.get(ticket)
        if mt5_pos is None:
            close_price = None
            close_source = POSITION_CLOSE_SOURCE_MT5_MISSING
            try:
                close_details = manager._trading.get_position_close_details(
                    ticket=ticket,
                    symbol=pos.symbol,
                )
            except Exception as exc:
                logger.debug(
                    "PositionManager: get_position_close_details(%s) error: %s",
                    ticket,
                    exc,
                )
                close_details = None
            if isinstance(close_details, dict):
                raw_close_price = close_details.get("close_price")
                if raw_close_price is not None:
                    try:
                        close_price = float(raw_close_price)
                        close_source = POSITION_CLOSE_SOURCE_HISTORY_DEALS
                    except (TypeError, ValueError):
                        close_price = None
            logger.info(
                "PositionManager: ticket=%d (%s %s) no longer open in MT5, "
                "removing (close_source=%s)",
                ticket,
                pos.action,
                pos.symbol,
                close_source,
            )
            manager.remove_position(ticket)
            pos.close_source = close_source
            for cb in list(manager._close_callbacks):
                try:
                    cb(pos, close_price)
                except Exception as cb_exc:
                    logger.warning(
                        "PositionManager: close callback error for ticket=%d: %s",
                        ticket,
                        cb_exc,
                    )
            if manager._on_position_closed is not None:
                manager._on_position_closed(pos, close_price)
            continue

        # 检测部分平仓
        mt5_volume = getattr(mt5_pos, "volume", None)
        if mt5_volume is not None:
            try:
                live_vol = float(mt5_volume)
                if live_vol > 0 and abs(live_vol - pos.volume) > 1e-6:
                    logger.info(
                        "PositionManager: partial close detected ticket=%d "
                        "volume %.2f→%.2f",
                        ticket,
                        pos.volume,
                        live_vol,
                    )
                    with manager._lock:
                        pos.volume = live_vol
                    if manager._on_position_updated is not None:
                        manager._on_position_updated(pos, POSITION_PARTIALLY_CLOSED)
            except (TypeError, ValueError):
                pass

        current_price = getattr(mt5_pos, "price_current", None)
        if current_price is not None:
            try:
                manager.update_price(ticket, float(current_price))
            except Exception as exc:
                logger.debug(
                    "PositionManager: update_price ticket=%d error: %s",
                    ticket,
                    exc,
                )
