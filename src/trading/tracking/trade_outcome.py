"""交易结果追踪器（TradeOutcomeTracker）。

## 职责

追踪 **实际执行的交易** 的盈亏结果。

## 与 SignalQualityTracker 的区别

- entry_price 来自实际成交价（由 TradeExecutor 下单后注入），不是 bar close
- 只追踪实际执行的交易，不追踪未执行的信号
- 由 TradeExecutor 主动登记（on_trade_opened），由 PositionManager 关仓触发评估
- 结果通知 StrategyPerformanceTracker（日内实时反馈）
- 可选写入 trade_outcomes 表

## 生命周期

1. TradeExecutor 下单成功 → on_trade_opened(signal_id, symbol, ..., fill_price, ...)
2. PositionManager 检测仓位关闭 → on_position_closed(pos, close_price)
3. 用 fill_price vs close_price 计算实际盈亏
4. 回调 PerformanceTracker + 写入 DB
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.signals.metadata_keys import MetadataKey as MK
from src.trading.trade_events import POSITION_CLOSE_SOURCE_MT5_MISSING

logger = logging.getLogger(__name__)


@dataclass
class _ActiveTrade:
    """一笔已执行但未平仓的交易记录。"""

    signal_id: str
    symbol: str
    timeframe: str
    strategy: str
    direction: str
    confidence: float
    fill_price: float
    regime: Optional[str]
    account_key: Optional[str]
    account_alias: Optional[str]
    intent_id: Optional[str]
    opened_at: datetime


class TradeOutcomeTracker:
    """追踪实际执行交易的盈亏结果。

    参数
    ----
    write_fn:
        接受 List[Tuple] 并写入 DB 的回调函数（写入 trade_outcomes 表）。
        None 表示不持久化（仅内存反馈）。
    on_outcome_fn:
        交易结果评估完成时的回调 (strategy, won, pnl, *, regime=, source="trade")。
        通常指向 StrategyPerformanceTracker.record_outcome。
    max_active:
        最大同时跟踪的活跃交易数（超出时丢弃最旧的）。
    """

    def __init__(
        self,
        *,
        write_fn: Optional[Callable[[List[Tuple]], None]] = None,
        on_outcome_fn: Optional[Callable[..., None]] = None,
        max_active: int = 200,
    ) -> None:
        self._write_fn = write_fn
        self._on_outcome_fn = on_outcome_fn
        self._max_active = max_active
        # key: signal_id → _ActiveTrade
        self._active: Dict[str, _ActiveTrade] = {}
        self._lock = threading.Lock()
        self._total_evaluated: int = 0
        self._total_wins: int = 0
        self._total_unresolved: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_trade_opened(
        self,
        signal_id: str,
        symbol: str,
        timeframe: str,
        strategy: str,
        direction: str,
        fill_price: float,
        confidence: float,
        regime: Optional[str] = None,
        account_key: Optional[str] = None,
        account_alias: Optional[str] = None,
        intent_id: Optional[str] = None,
        *,
        opened_at: Optional[datetime] = None,
    ) -> None:
        """TradeExecutor 下单成功后调用，登记活跃交易。"""
        trade = _ActiveTrade(
            signal_id=signal_id,
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            direction=direction,
            confidence=confidence,
            fill_price=fill_price,
            regime=regime,
            account_key=account_key,
            account_alias=account_alias,
            intent_id=intent_id,
            opened_at=opened_at or datetime.now(timezone.utc),
        )
        with self._lock:
            self._active[signal_id] = trade
            # 超出上限时丢弃最旧的（优先丢弃超过 24h 的条目）
            if len(self._active) > self._max_active:
                now = datetime.now(timezone.utc)
                stale_cutoff = now - timedelta(hours=24)
                stale_keys = [
                    k for k, v in self._active.items()
                    if v.opened_at < stale_cutoff
                ]
                if stale_keys:
                    for k in stale_keys[:len(self._active) - self._max_active]:
                        removed = self._active.pop(k, None)
                        if removed:
                            self._total_unresolved += 1
                            logger.warning(
                                "TradeOutcomeTracker: evicting stale trade %s (opened %s)",
                                k, removed.opened_at.isoformat(),
                            )
                elif len(self._active) > self._max_active:
                    oldest_key = min(
                        self._active, key=lambda k: self._active[k].opened_at
                    )
                    removed = self._active.pop(oldest_key, None)
                    if removed:
                        self._total_unresolved += 1
                        logger.warning(
                            "TradeOutcomeTracker: force-evicting oldest trade %s", oldest_key,
                        )
        logger.debug(
            "TradeOutcomeTracker: registered trade signal_id=%s %s %s @ %.5f",
            signal_id, direction, symbol, fill_price,
        )

    def restore_tracked_position(self, pos: Any) -> None:
        signal_id = str(getattr(pos, "signal_id", "") or "").strip()
        if not signal_id:
            return
        self.on_trade_opened(
            signal_id=signal_id,
            symbol=str(getattr(pos, "symbol", "") or ""),
            timeframe=str(getattr(pos, "timeframe", "") or ""),
            strategy=str(getattr(pos, "strategy", "") or ""),
            direction=str(getattr(pos, "action", "") or ""),
            fill_price=float(getattr(pos, "entry_price", 0.0) or 0.0),
            confidence=float(getattr(pos, "confidence", 0.0) or 0.0),
            regime=getattr(pos, "regime", None),
            account_key=getattr(pos, "account_key", None),
            account_alias=getattr(pos, "account_alias", None),
            intent_id=getattr(pos, "intent_id", None),
            opened_at=getattr(pos, "opened_at", None),
        )

    def on_position_closed(
        self,
        pos: Any,
        close_price: Optional[float],
    ) -> None:
        """PositionManager 关仓回调 — 用实际成交价立即评估。

        参数
        ----
        pos:
            TrackedPosition 实例（含 signal_id, symbol, action）。
            若 ``pos.close_source`` 存在，作为关仓来源标签。
        close_price:
            仓位关闭时的实际价格；None 表示无法获取（记录为 unresolved）。
        """
        # §0y P2：旧实现读 pos._close_source 但 TrackedPosition 公开字段是
        # close_source（manager.py:116 + reconciliation.py:376），真实关仓来源
        # 静默被丢失 → metadata 落库永远为默认 'position_closed'，污染审计事实。
        close_source: str = getattr(pos, "close_source", "position_closed") or "position_closed"
        signal_id: str = getattr(pos, "signal_id", "") or ""
        if not signal_id:
            return

        # 解析 close_price
        close_price_f: Optional[float] = None
        if close_price is not None:
            try:
                close_price_f = float(close_price)
            except (TypeError, ValueError):
                close_price_f = None

        trade: Optional[_ActiveTrade] = None
        with self._lock:
            trade = self._active.pop(signal_id, None)

        if trade is None:
            return

        # 计算盈亏（close_price 缺失时标记 unresolved）
        won: Optional[bool] = None
        price_change: Optional[float] = None
        if close_price_f is not None:
            price_change = close_price_f - trade.fill_price
            if trade.direction == "buy":
                won = price_change > 0
            elif trade.direction == "sell":
                won = price_change < 0

        with self._lock:
            if close_price_f is None:
                self._total_unresolved += 1
            elif won is not None:
                self._total_evaluated += 1
                if won:
                    self._total_wins += 1

        # 实时绩效回调（仅已解析的交易）
        if won is not None and price_change is not None and self._on_outcome_fn is not None:
            try:
                exit_reason = getattr(pos, "last_exit_reason", "") or ""
                self._on_outcome_fn(
                    trade.strategy, won, price_change,
                    regime=trade.regime,
                    source="trade",
                    exit_reason=exit_reason,
                )
            except Exception:
                logger.debug(
                    "TradeOutcomeTracker: on_outcome_fn callback failed for %s",
                    trade.strategy, exc_info=True,
                )

        if close_price_f is None:
            logger.warning(
                "TradeOutcomeTracker: unresolved close for signal_id=%s %s %s "
                "(close_price unavailable, source=%s)",
                signal_id, trade.direction, trade.symbol, close_source,
            )

        # DB 持久化（包括 unresolved，便于事后审计）
        if self._write_fn is not None:
            now = datetime.now(timezone.utc)
            # 从 TrackedPosition 提取 Chandelier Exit 追溯字段
            exit_reason = getattr(pos, "last_exit_reason", "") or ""
            r_multiple = getattr(pos, "last_r_multiple", None)
            exit_regime = getattr(pos, "last_exit_regime", "") or ""
            metadata: Dict[str, Any] = {
                "opened_at": trade.opened_at.isoformat(),
                "close_source": close_source if close_price_f is not None else POSITION_CLOSE_SOURCE_MT5_MISSING,
            }
            if exit_reason:
                metadata[MK.EXIT_REASON] = exit_reason
            if r_multiple is not None:
                metadata[MK.R_MULTIPLE] = round(float(r_multiple), 4)
            if exit_regime:
                metadata[MK.EXIT_REGIME] = exit_regime
            rows: List[Tuple] = [(
                now,
                trade.signal_id,
                trade.account_key,
                trade.account_alias,
                trade.intent_id,
                trade.symbol,
                trade.timeframe,
                trade.strategy,
                trade.direction,
                trade.confidence,
                trade.fill_price,
                close_price_f,
                price_change,
                won,
                trade.regime,
                metadata,
            )]
            try:
                self._write_fn(rows)
            except Exception:
                logger.exception(
                    "TradeOutcomeTracker: failed to write trade outcome for %s",
                    trade.signal_id,
                )

    def summary(self) -> Dict[str, Any]:
        """返回内存中的实时交易结果统计。"""
        with self._lock:
            return {
                "total_evaluated": self._total_evaluated,
                "total_wins": self._total_wins,
                "win_rate": (
                    round(self._total_wins / self._total_evaluated, 4)
                    if self._total_evaluated > 0
                    else None
                ),
                "active_trades": len(self._active),
                "unresolved_closes": self._total_unresolved,
            }
