"""SL/TP 修改操作（从 PositionManager 提取的纯函数模块）。

所有函数接收 manager 引用作为显式参数（ADR-002 模式）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..trade_events import (
    POSITION_UPDATE_REASON_TRAILING_SL,
    POSITION_UPDATE_REASON_TRAILING_TP,
)

if TYPE_CHECKING:
    from .manager import PositionManager, TrackedPosition

logger = logging.getLogger(__name__)


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
    """执行 SL 修改并记录历史。"""
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
                    failed[0].get("retcode")
                    if isinstance(failed[0], dict)
                    else None
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
                    failed[0].get("retcode")
                    if isinstance(failed[0], dict)
                    else None
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
