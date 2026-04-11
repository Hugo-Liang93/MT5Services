"""预交易过滤链（从 TradeExecutor 提取的纯函数模块）。

12 层串联检查，按顺序决定信号是否应执行交易。
所有函数接收 executor 引用作为显式参数（ADR-002 模式）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.signals.metadata_keys import MetadataKey as MK
from .reasons import (
    REASON_CIRCUIT_OPEN,
    REASON_DUPLICATE_SIGNAL_ID,
    REASON_EQUITY_CURVE_BELOW_MA,
    REASON_EOD_BLOCK,
    REASON_INVALID_DIRECTION,
    REASON_LIMIT_REACHED,
    REASON_MARGIN_GUARD_BLOCK,
    REASON_MIN_CONFIDENCE,
    REASON_PERFORMANCE_PAUSED,
    REASON_REENTRY_COOLDOWN,
    SKIP_CATEGORY_COOLDOWN,
    SKIP_CATEGORY_PERFORMANCE,
    SKIP_CATEGORY_EQUITY_FILTER,
    SKIP_CATEGORY_RISK_GUARD,
    SKIP_CATEGORY_EXECUTION_GATE,
    SKIP_CATEGORY_DUPLICATE_GUARD,
    SKIP_CATEGORY_CONFIDENCE,
    SKIP_CATEGORY_EOD_GUARD,
    SKIP_CATEGORY_POSITION,
)

from .eventing import emit_execution_blocked as _emit_execution_blocked_helper
from .eventing import notify_skip as _notify_skip_helper
from .params import tf_to_seconds as _tf_to_seconds_helper
from .pending_orders import (
    duplicate_execution_reason as _duplicate_execution_reason_helper,
)
from .pending_orders import reached_position_limit as _reached_position_limit_helper

if TYPE_CHECKING:
    from src.signals.models import SignalEvent

    from .executor import TradeExecutor

logger = logging.getLogger(__name__)


def reject_signal(
    executor: TradeExecutor,
    event: SignalEvent,
    reason: str,
    category: str,
    tf: str,
    *,
    log_level: str = "info",
    extra_log: str = "",
    pipeline_reason: str = "",
) -> None:
    """统一拒绝信号：日志 + 事件总线 + skip 通知 + 执行日志。"""
    msg = (
        f"TradeExecutor: skipping {event.symbol}/{event.strategy} "
        f"{event.direction} - {reason}"
    )
    if extra_log:
        msg = f"{msg} ({extra_log})"
    if log_level == "error":
        logger.error(msg)
    elif log_level == "warning":
        logger.warning(msg)
    elif log_level == "debug":
        logger.debug(msg)
    else:
        logger.info(msg)
    executor.execution_log.append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "signal_id": event.signal_id,
            "symbol": event.symbol,
            "direction": event.direction,
            "strategy": event.strategy,
            "success": False,
            "skipped": True,
            "reason": reason,
        }
    )
    _notify_skip_helper(executor, event.signal_id, reason, tf, event=event)
    _emit_execution_blocked_helper(
        executor,
        event,
        reason=pipeline_reason or reason,
        category=category,
    )


def check_trading_health(executor: TradeExecutor) -> bool:
    """熔断器自动恢复前的健康检查：验证 MT5 连接和账户可用。"""
    try:
        info = executor.trading.account_info()
        return info is not None
    except Exception as exc:
        logger.debug("Trading health check failed: %s", exc)
        return False


def check_circuit_breaker(executor: TradeExecutor, event: SignalEvent) -> bool:
    """检查技术故障熔断器。返回 True = 已熔断，应拒绝。"""
    if not executor.circuit_open:
        return False
    # 超过自动恢复窗口后，先做健康检查再决定是否 half-open。
    if (
        executor.config.circuit_auto_reset_minutes > 0
        and executor.circuit_open_at is not None
    ):
        elapsed = (
            datetime.now(timezone.utc) - executor.circuit_open_at
        ).total_seconds() / 60.0
        if elapsed >= executor.config.circuit_auto_reset_minutes:
            if check_trading_health(executor):
                logger.info(
                    "TradeExecutor: circuit auto-reset after %.1f minutes, "
                    "health check passed, entering half-open",
                    elapsed,
                )
                executor.health_check_failures = 0
                executor.reset_circuit()
            else:
                executor.health_check_failures += 1
                executor.circuit_open_at = datetime.now(timezone.utc)
                if (
                    executor.health_check_failures
                    >= executor.max_health_check_failures
                ):
                    logger.critical(
                        "TradeExecutor: circuit STUCK — %d consecutive health "
                        "check failures, MT5 connection may be permanently "
                        "broken. Manual intervention required (reset_circuit "
                        "or restart).",
                        executor.health_check_failures,
                    )
                else:
                    logger.warning(
                        "TradeExecutor: circuit auto-reset deferred, "
                        "health check failed after %.1f minutes "
                        "(health_check_failures=%d/%d)",
                        elapsed,
                        executor.health_check_failures,
                        executor.max_health_check_failures,
                    )
    if executor.circuit_open:
        logger.warning(
            "TradeExecutor: circuit open (consecutive_failures=%d), "
            "skipping %s/%s. Call reset_circuit() to resume.",
            executor.consecutive_failures,
            event.symbol,
            event.strategy,
        )
        return True
    return False


def check_reentry_cooldown(
    executor: TradeExecutor, event: SignalEvent, tf: str
) -> bool:
    """检查同策略同方向再入场冷却。返回 True = 冷却中，应拒绝。"""
    cooldown_bars = executor.config.reentry_cooldown_bars
    if cooldown_bars <= 0:
        return False
    reentry_key = (event.symbol, event.strategy, event.direction)
    bar_time_raw = event.metadata.get(MK.BAR_TIME)
    bar_time: datetime | None = None
    if isinstance(bar_time_raw, datetime):
        bar_time = bar_time_raw
    elif isinstance(bar_time_raw, str):
        try:
            bar_time = datetime.fromisoformat(bar_time_raw)
        except (ValueError, TypeError):
            pass
    last_bar = executor.last_entry_bar_time.get(reentry_key)
    if bar_time and last_bar:
        tf_seconds = _tf_to_seconds_helper(tf)
        if tf_seconds > 0:
            elapsed_bars = abs((bar_time - last_bar).total_seconds()) / tf_seconds
            if elapsed_bars < cooldown_bars:
                reject_signal(
                    executor,
                    event,
                    REASON_REENTRY_COOLDOWN,
                    SKIP_CATEGORY_COOLDOWN,
                    tf,
                    extra_log=f"{elapsed_bars:.1f} bars < {cooldown_bars} required",
                )
                return True
    return False


def run_pre_trade_filters(
    executor: TradeExecutor, event: SignalEvent, tf: str
) -> str | None:
    """按顺序运行所有预交易过滤器。

    返回 None = 全部通过；返回 str = 拒绝原因。
    """
    sig_id = event.signal_id or ""

    # ① signal_id 幂等性
    if sig_id and sig_id in executor.executed_signal_ids:
        _notify_skip_helper(
            executor, sig_id, REASON_DUPLICATE_SIGNAL_ID, tf, event=event
        )
        return REASON_DUPLICATE_SIGNAL_ID

    # ② 技术故障熔断器
    if check_circuit_breaker(executor, event):
        return REASON_CIRCUIT_OPEN

    # ③ 方向有效性
    if event.direction not in ("buy", "sell"):
        return REASON_INVALID_DIRECTION

    # ④ PnL 熔断
    if (
        executor.performance_tracker is not None
        and executor.performance_tracker.is_trading_paused()
    ):
        reject_signal(
            executor,
            event,
            REASON_PERFORMANCE_PAUSED,
            SKIP_CATEGORY_PERFORMANCE,
            tf,
            log_level="warning",
        )
        return REASON_PERFORMANCE_PAUSED

    # ⑤ 权益曲线过滤器
    if executor.equity_curve_filter is not None:
        executor.equity_curve_filter.record_equity()
        if executor.equity_curve_filter.should_block():
            reject_signal(
                executor,
                event,
                REASON_EQUITY_CURVE_BELOW_MA,
                SKIP_CATEGORY_EQUITY_FILTER,
                tf,
            )
            return REASON_EQUITY_CURVE_BELOW_MA

    # ⑥ 保证金保护
    if (
        executor.margin_guard is not None
        and executor.margin_guard.should_block_new_trades()
    ):
        reject_signal(
            executor,
            event,
            REASON_MARGIN_GUARD_BLOCK,
            SKIP_CATEGORY_RISK_GUARD,
            tf,
        )
        return REASON_MARGIN_GUARD_BLOCK

    # ⑦ 执行门（voting group / armed）
    gate_allowed, gate_reason = executor.execution_gate.check(event)
    if not gate_allowed:
        reject_signal(
            executor,
            event,
            gate_reason,
            SKIP_CATEGORY_EXECUTION_GATE,
            tf,
        )
        return gate_reason

    # ⑧ 重复执行上下文
    duplicate_reason = _duplicate_execution_reason_helper(executor, event)
    if duplicate_reason:
        reject_signal(
            executor, event, duplicate_reason, SKIP_CATEGORY_DUPLICATE_GUARD, tf
        )
        return duplicate_reason

    # ⑨ 最小置信度
    effective_min_conf = executor.config.timeframe_min_confidence.get(
        tf,
        executor.config.min_confidence,
    )
    if event.confidence < effective_min_conf:
        reject_signal(
            executor,
            event,
            REASON_MIN_CONFIDENCE,
            SKIP_CATEGORY_CONFIDENCE,
            tf,
            extra_log=f"{event.confidence:.3f} < {effective_min_conf:.2f}",
        )
        return REASON_MIN_CONFIDENCE

    # ⑩ EOD 后禁止新开仓
    if (
        executor.position_manager is not None
        and executor.position_manager.is_after_eod_today()
    ):
        reject_signal(
            executor, event, REASON_EOD_BLOCK, SKIP_CATEGORY_EOD_GUARD, tf
        )
        return REASON_EOD_BLOCK

    # ⑪ 品种持仓数量上限
    if _reached_position_limit_helper(executor, event.symbol):
        reject_signal(
            executor,
            event,
            REASON_LIMIT_REACHED,
            SKIP_CATEGORY_POSITION,
            tf,
            pipeline_reason=REASON_LIMIT_REACHED,
        )
        return REASON_LIMIT_REACHED

    # ⑫ 再入场冷却
    if check_reentry_cooldown(executor, event, tf):
        return REASON_REENTRY_COOLDOWN

    return None
