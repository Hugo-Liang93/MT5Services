"""SL/TP 修改操作（从 PositionManager 提取的纯函数模块）。

所有函数接收 manager 引用作为显式参数（ADR-002 模式）。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ..trade_events import (
    POSITION_UPDATE_REASON_TRAILING_SL,
    POSITION_UPDATE_REASON_TRAILING_TP,
)

if TYPE_CHECKING:
    from .manager import PositionManager, TrackedPosition

logger = logging.getLogger(__name__)

# (ticket → last warning timestamp) 用于 throttle Invalid stops noise。
# chandelier_trail 撞 broker stop level 时会每个评估周期重试，相同 ticket
# 在 5min 内仅记录一次 WARNING，后续降 DEBUG 静默。ticket 平仓时
# manager.remove_position() 调用 forget_ticket_invalid_stops 清理（reconciliation
# 是 remove_position 的触发源之一）。
_logged_invalid_stops: dict[int, float] = {}
_INVALID_STOPS_LOG_THROTTLE_SECONDS = 300.0


def forget_ticket_invalid_stops(ticket: int) -> None:
    """ticket 平仓后清理 throttle 记忆。"""
    _logged_invalid_stops.pop(ticket, None)


def get_current_atr(manager: PositionManager, pos: "TrackedPosition") -> float:
    """从 IndicatorManager 读取当前 ATR（已计算好的缓存值）。"""
    if manager._indicator_source is None:
        return pos.atr_at_entry
    atr_data = manager._indicator_source.get_indicator(
        pos.symbol,
        pos.timeframe,
        "atr14",
    )
    if isinstance(atr_data, dict):
        val = atr_data.get("atr")
        if val is not None and float(val) > 0:
            return float(val)
    return pos.atr_at_entry


def default_atr_from_position(price_open: float, stop_loss: float) -> float:
    """从入场价和止损价反推 ATR 近似值。"""
    try:
        if stop_loss:
            sl_distance = abs(float(price_open) - float(stop_loss))
            return sl_distance / 2.0
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def modify_sl(
    manager: PositionManager,
    pos: "TrackedPosition",
    new_sl: float,
    reason: str = POSITION_UPDATE_REASON_TRAILING_SL,
) -> bool:
    """执行 SL 修改并记录历史。

    chandelier_trail 在每次评估周期（~10s）都试图把 SL 拉到当前价附近，
    broker 的 stop level 检查会拒绝 SL 距 current price 过近的修改
    （retcode 10016 INVALID_STOPS）。trail 一旦撞 broker stop level，
    后续每个周期都会重试同样太近的距离 → 持续刷 WARNING。

    本函数对相同 (ticket, retcode=10016) 失败做 5min throttle，第一次报
    WARNING，后续 DEBUG 静默——避免 errors.log 被同一 ticket 的 trail 失败
    淹没。ticket 平仓时由 reconciliation 路径清理 _logged_invalid_stops。
    """
    old_sl = pos.stop_loss
    target_sl = round(new_sl, 2)
    retcode: int | None = None
    broker_comment: str = ""
    success = False
    try:
        result = manager._trading.modify_positions(
            ticket=pos.ticket, symbol=pos.symbol, sl=target_sl
        )
        if isinstance(result, dict):
            modified_list = result.get("modified", [])
            modified_tickets = {
                int(item["ticket"]) if isinstance(item, dict) else int(item)
                for item in modified_list
                if item is not None
            }
            if modified_list and isinstance(modified_list[0], dict):
                retcode = modified_list[0].get("retcode")
                broker_comment = modified_list[0].get("comment", "")
            if modified_tickets and pos.ticket not in modified_tickets:
                raise RuntimeError(f"ticket {pos.ticket} not modified")
            failed = list(result.get("failed", []) or [])
            if failed and pos.ticket not in modified_tickets:
                retcode = (
                    failed[0].get("retcode") if isinstance(failed[0], dict) else None
                )
                broker_comment = (
                    str(failed[0].get("error", ""))
                    if isinstance(failed[0], dict)
                    else str(failed[0])
                )
                raise RuntimeError(broker_comment)
        pos.stop_loss = target_sl
        success = True
        return True
    except Exception as exc:
        if retcode == 10016:  # TRADE_RETCODE_INVALID_STOPS
            now_ts = time.time()
            last_logged = _logged_invalid_stops.get(pos.ticket, 0.0)
            if now_ts - last_logged < _INVALID_STOPS_LOG_THROTTLE_SECONDS:
                logger.debug(
                    "Failed to modify SL for ticket=%d: %s (throttled)",
                    pos.ticket,
                    exc,
                )
            else:
                logger.warning(
                    "Failed to modify SL for ticket=%d: %s "
                    "(broker stop level; throttling next 5min)",
                    pos.ticket,
                    exc,
                )
                _logged_invalid_stops[pos.ticket] = now_ts
        else:
            logger.warning("Failed to modify SL for ticket=%d: %s", pos.ticket, exc)
        if not broker_comment:
            broker_comment = str(exc)
        return False
    finally:
        manager._record_sl_tp_change(
            pos,
            reason=reason,
            action_type="modify_sl",
            old_sl=old_sl,
            new_sl=target_sl,
            old_tp=None,
            new_tp=None,
            success=success,
            retcode=retcode,
            broker_comment=broker_comment,
        )


def modify_tp(
    manager: PositionManager,
    pos: "TrackedPosition",
    new_tp: float,
    reason: str = POSITION_UPDATE_REASON_TRAILING_TP,
) -> bool:
    """执行 TP 修改并记录历史。"""
    old_tp = pos.take_profit
    target_tp = round(new_tp, 2)
    retcode: int | None = None
    broker_comment: str = ""
    success = False
    try:
        result = manager._trading.modify_positions(
            ticket=pos.ticket, symbol=pos.symbol, tp=target_tp
        )
        if isinstance(result, dict):
            modified_list = result.get("modified", [])
            modified_tickets = {
                int(item["ticket"]) if isinstance(item, dict) else int(item)
                for item in modified_list
                if item is not None
            }
            if modified_list and isinstance(modified_list[0], dict):
                retcode = modified_list[0].get("retcode")
                broker_comment = modified_list[0].get("comment", "")
            if modified_tickets and pos.ticket not in modified_tickets:
                raise RuntimeError(f"ticket {pos.ticket} not modified")
            failed = list(result.get("failed", []) or [])
            if failed and pos.ticket not in modified_tickets:
                retcode = (
                    failed[0].get("retcode") if isinstance(failed[0], dict) else None
                )
                broker_comment = (
                    str(failed[0].get("error", ""))
                    if isinstance(failed[0], dict)
                    else str(failed[0])
                )
                raise RuntimeError(broker_comment)
        pos.take_profit = target_tp
        success = True
        return True
    except Exception as exc:
        logger.warning("Failed to modify TP for ticket=%d: %s", pos.ticket, exc)
        if not broker_comment:
            broker_comment = str(exc)
        return False
    finally:
        manager._record_sl_tp_change(
            pos,
            reason=reason,
            action_type="modify_tp",
            old_sl=None,
            new_sl=None,
            old_tp=old_tp,
            new_tp=target_tp,
            success=success,
            retcode=retcode,
            broker_comment=broker_comment,
        )
