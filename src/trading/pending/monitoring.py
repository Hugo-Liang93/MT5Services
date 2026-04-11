"""PendingEntry 的入场监控与成交触发子模块。"""

from __future__ import annotations

import dataclasses
import logging
import queue
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..reasons import REASON_FILL_QUEUE_OVERFLOW, REASON_PENDING_TIMEOUT

logger = logging.getLogger(__name__)


class PendingEntryMonitoringService:
    """封装挂起入场的价格监控、超时处理、填单分发。"""

    def __init__(
        self,
        *,
        stop_event: Any,
        pending: dict[str, Any],
        lock: Any,
        fill_queue: queue.Queue[Any],
        market_service: Any,
        execute_fn: Callable[[Any, Any, dict[str, Any]], Any],
        on_expired_fn: Optional[Callable[[str, str], None]],
        extract_quote_prices: Callable[[Any], Optional[tuple[float, float]]],
        fill_result_factory: Callable[[Any, Any, dict[str, Any]], Any],
        stats: dict[str, Any],
        check_interval: float,
        max_spread_points: float,
        timeout_fill_tolerance_atr: float,
    ) -> None:
        self._stop_event = stop_event
        self._pending = pending
        self._lock = lock
        self._fill_queue = fill_queue
        self._market = market_service
        self._execute_fn = execute_fn
        self._on_expired_fn = on_expired_fn
        self._extract_quote_prices = extract_quote_prices
        self._fill_result_factory = fill_result_factory
        self._stats = stats
        self._check_interval = float(check_interval)
        self._max_spread_points = float(max_spread_points)
        self._timeout_fill_tolerance_atr = float(timeout_fill_tolerance_atr)

    def set_runtime_config(
        self,
        *,
        check_interval: float,
        max_spread_points: float,
        timeout_fill_tolerance_atr: Optional[float] = None,
    ) -> None:
        self._check_interval = float(check_interval)
        self._max_spread_points = float(max_spread_points)
        if timeout_fill_tolerance_atr is not None:
            self._timeout_fill_tolerance_atr = float(timeout_fill_tolerance_atr)

    def run_fill_worker(self) -> None:
        while not self._stop_event.is_set() or not self._fill_queue.empty():
            try:
                result = self._fill_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._execute_fn(
                    result.signal_event,
                    result.trade_params,
                    result.cost_metrics,
                )
            except Exception:
                logger.error(
                    "PendingEntry execute failed after fill: %s",
                    result.signal_event.signal_id,
                    exc_info=True,
                )
            finally:
                self._fill_queue.task_done()

    def clear_fill_queue(self) -> None:
        while True:
            try:
                self._fill_queue.get_nowait()
                self._fill_queue.task_done()
            except queue.Empty:
                break

    def check_all_entries(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            entries = [e for e in self._pending.values() if e.status == "pending"]

        for entry in entries:
            if now >= entry.expires_at:
                self.expire_entry(entry)
                continue

            quote = self._market.get_quote(entry.signal_event.symbol)
            if quote is None:
                continue

            self.check_price(entry, quote)

    def check_price(self, entry: Any, quote: Any) -> None:
        prices = self._extract_quote_prices(quote)
        if prices is None:
            return

        bid, ask = prices
        direction = entry.signal_event.direction
        # BUY → 用 ask（实际买入价），SELL → 用 bid（实际卖出价）
        check_price = ask if direction == "buy" else bid

        with self._lock:
            if entry.signal_event.signal_id not in self._pending:
                return
            if entry.status != "pending":
                return

            entry.checks_count += 1

            if entry.best_price_seen is None:
                entry.best_price_seen = check_price
            elif direction == "buy":
                entry.best_price_seen = min(entry.best_price_seen, check_price)
            else:
                entry.best_price_seen = max(entry.best_price_seen, check_price)

        if not (entry.entry_low <= check_price <= entry.entry_high):
            return

        if self._max_spread_points > 0:
            spread_points = self.get_spread_points(bid, ask, entry.signal_event.symbol)
            if (
                spread_points is not None
                and spread_points > self._max_spread_points
            ):
                logger.debug(
                    "PendingEntry %s: spread %.1f > max %.1f, skip this check",
                    entry.signal_event.signal_id[:8],
                    spread_points,
                    self._max_spread_points,
                )
                return

        self.fill_entry(entry, check_price)

    def get_spread_points(self, bid: float, ask: float, symbol: str) -> Optional[float]:
        try:
            point = self._market.get_symbol_point(symbol)
            if point is None or point <= 0:
                return None
            return abs(ask - bid) / point
        except Exception:
            return None

    def fill_entry(self, entry: Any, fill_price: float) -> None:
        with self._lock:
            if self._pending.pop(entry.signal_event.signal_id, None) is None:
                return
            entry.status = "filled"
            entry.fill_price = fill_price

            self._stats["total_filled"] = self._stats.get("total_filled", 0) + 1
            if entry.signal_event.direction == "buy":
                improvement = entry.reference_price - fill_price
            else:
                improvement = fill_price - entry.reference_price
            self._stats["total_price_improvement"] = (
                self._stats.get("total_price_improvement", 0.0) + improvement
            )

        logger.info(
            "PendingEntry filled: %s/%s %s @ %.2f (ref=%.2f, improve=%.2f, "
            "waited=%ds, checks=%d)",
            entry.signal_event.symbol,
            entry.signal_event.strategy,
            entry.signal_event.direction,
            fill_price,
            entry.reference_price,
            improvement,
            (datetime.now(timezone.utc) - entry.created_at).total_seconds(),
            entry.checks_count,
        )

        price_shift = fill_price - entry.trade_params.entry_price
        updated_params = dataclasses.replace(
            entry.trade_params,
            entry_price=fill_price,
            stop_loss=round(entry.trade_params.stop_loss + price_shift, 2),
            take_profit=round(entry.trade_params.take_profit + price_shift, 2),
        )

        fill_result = self._fill_result_factory(
            signal_event=entry.signal_event,
            trade_params=updated_params,
            cost_metrics=entry.cost_metrics,
        )
        try:
            self._fill_queue.put_nowait(fill_result)
        except queue.Full:
            logger.warning(
                "PendingEntry fill queue full, blocking for %s",
                entry.signal_event.signal_id,
            )
            try:
                self._fill_queue.put(fill_result, timeout=5.0)
            except queue.Full:
                logger.error(
                    "PendingEntry fill queue still full after 5s, fill LOST for %s. "
                    "This indicates fill_worker is stuck or too slow.",
                    entry.signal_event.signal_id,
                )
                with self._lock:
                    self._pending.pop(entry.signal_event.signal_id, None)
                    entry.status = "expired"
                    entry.cancel_reason = REASON_FILL_QUEUE_OVERFLOW
                    self._stats["total_expired"] = self._stats.get(
                        "total_expired", 0
                    ) + 1

    def expire_entry(self, entry: Any) -> None:
        timeout_tolerance_ratio = self._timeout_fill_tolerance_atr
        if timeout_tolerance_ratio > 0 and entry.trade_params.atr_value > 0:
            quote = self._market.get_quote(entry.signal_event.symbol)
            if quote is not None:
                prices = self._extract_quote_prices(quote)
                if prices is not None:
                    bid, ask = prices
                    check_price = ask if entry.signal_event.direction == "buy" else bid
                    distance = abs(check_price - entry.reference_price)
                    threshold = entry.trade_params.atr_value * timeout_tolerance_ratio
                    if distance <= threshold and timeout_tolerance_ratio > 0:
                        logger.info(
                            "PendingEntry timeout fill tolerance: %s/%s %s @ %.2f "
                            "(ref=%.2f, dist=%.2f < threshold=%.2f)",
                            entry.signal_event.symbol,
                            entry.signal_event.strategy,
                            entry.signal_event.direction,
                            check_price,
                            entry.reference_price,
                            distance,
                            threshold,
                        )
                        self.fill_entry(entry, check_price)
                        return

        with self._lock:
            if self._pending.pop(entry.signal_event.signal_id, None) is None:
                return
            entry.status = "expired"
            entry.cancel_reason = REASON_PENDING_TIMEOUT
            self._stats["total_expired"] = self._stats.get("total_expired", 0) + 1

        logger.info(
            "PendingEntry expired: %s/%s %s best_seen=%.2f zone=[%.2f, %.2f] checks=%d",
            entry.signal_event.symbol,
            entry.signal_event.strategy,
            entry.signal_event.direction,
            entry.best_price_seen or 0.0,
            entry.entry_low,
            entry.entry_high,
            entry.checks_count,
        )

        if self._on_expired_fn:
            try:
                self._on_expired_fn(entry.signal_event.signal_id, "pending_expired")
            except Exception:
                logger.warning(
                    "on_expired_fn callback failed for expired signal %s",
                    entry.signal_event.signal_id,
                    exc_info=True,
                )

    def monitor_interval(self) -> float:
        return self._check_interval
