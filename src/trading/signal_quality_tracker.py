"""信号质量追踪器（SignalQualityTracker）。

## 职责

评估 **信号本身的预测质量**，不关心是否实际执行了交易。
每个 confirmed_buy/confirmed_sell 信号在 N 根 bar 后用收盘价评估方向是否正确。
结果写入 signal_outcomes 表，供 ConfidenceCalibrator 定期查询做长期统计校准。

## 与 TradeOutcomeTracker 的区别

| 维度 | SignalQualityTracker | TradeOutcomeTracker |
|------|---------------------|---------------------|
| 衡量什么 | 信号预测质量 | 实际交易盈亏 |
| entry_price | 信号 bar 的 close（理论入场） | 实际成交价（含滑点） |
| exit_price | N bars 后的 close（理论退出） | 实际平仓价 |
| 评估对象 | 所有 confirmed 信号（含未执行的）| 仅实际执行的交易 |
| 消费者 | ConfidenceCalibrator（长期统计校准）| StrategyPerformanceTracker（日内反馈）|

## 集成方式

SignalQualityTracker 作为 SignalRuntime 的信号监听器注册：
    quality_tracker.attach(runtime) → runtime.add_signal_listener(on_signal_event)

每次 scope="confirmed" 的 SignalEvent 到来时：
    1. _advance_pending：推进同一 (symbol, timeframe) 下所有策略的 pending 计数
    2. _record_pending：若为 confirmed_buy/sell，登记新的待评估信号
    3. 当 bars_elapsed >= bars_to_evaluate，用最新 close 作为 exit_price 计算胜负，写入 DB
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class _PendingSignal:
    """等待回填的未结信号记录。"""

    __slots__ = (
        "signal_id", "symbol", "timeframe", "strategy", "action",
        "confidence", "entry_price", "issued_at", "bar_time",
        "regime", "bars_elapsed",
    )

    def __init__(
        self,
        signal_id: str,
        symbol: str,
        timeframe: str,
        strategy: str,
        action: str,
        confidence: float,
        entry_price: Optional[float],
        issued_at: datetime,
        bar_time: datetime,
        regime: Optional[str],
    ) -> None:
        self.signal_id = signal_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.strategy = strategy
        self.action = action
        self.confidence = confidence
        self.entry_price = entry_price
        self.issued_at = issued_at
        self.bar_time = bar_time
        self.regime = regime
        self.bars_elapsed: int = 0


class SignalQualityTracker:
    """在 N 根 bar 后回填信号的理论盈亏结果（纯信号质量评估）。

    参数
    ----
    write_fn:
        接受 List[Tuple] 并写入 DB 的回调函数（写入 signal_outcomes 表）。
    bars_to_evaluate:
        发出信号后等待多少根 bar 再判定输赢（默认 5）。
    max_pending:
        内存中最多保留多少个待评估信号（超出时丢弃最旧的）。
    on_quality_fn:
        信号评估完成时的可选回调 (strategy, won, pnl, *, regime=)。
    """

    def __init__(
        self,
        write_fn: Callable[[List[Tuple]], None],
        *,
        bars_to_evaluate: int = 5,
        max_pending: int = 500,
        on_quality_fn: Optional[Callable[..., None]] = None,
    ) -> None:
        self._write_fn = write_fn
        self._bars_to_evaluate = max(1, bars_to_evaluate)
        self._max_pending = max_pending
        self._on_quality_fn = on_quality_fn
        # key: (symbol, timeframe, strategy) → list of pending signals
        self._pending: Dict[Tuple[str, str, str], List[_PendingSignal]] = {}
        self._lock = threading.Lock()
        self._total_evaluated: int = 0
        self._total_wins: int = 0
        # 去重：同一 (symbol, timeframe) 同一 bar_time 只推进一次，
        # 防止同一根 bar 的多个策略事件重复推进 bars_elapsed。
        self._last_advance_bar: Dict[Tuple[str, str], Any] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_signal_event(self, event: Any) -> None:
        """SignalRuntime 信号监听回调。

        - confirmed_buy / confirmed_sell → 记录待评估信号
        - 后续 confirmed snapshot 到来时 → 推进计数并回填结果
        """
        signal_state = str(event.metadata.get("signal_state", ""))
        scope = str(event.metadata.get("scope", ""))

        if scope != "confirmed":
            return

        self._advance_pending(event)
        if signal_state in ("confirmed_buy", "confirmed_sell"):
            self._record_pending(event)

    def winrate_summary(self) -> Dict[str, Any]:
        """返回内存中统计的信号质量实时胜率（不依赖 DB）。"""
        with self._lock:
            return {
                "total_evaluated": self._total_evaluated,
                "total_wins": self._total_wins,
                "win_rate": (
                    round(self._total_wins / self._total_evaluated, 4)
                    if self._total_evaluated > 0
                    else None
                ),
                "pending_count": sum(len(v) for v in self._pending.values()),
            }

    def on_execution_skip(self, signal_id: str, reason: str) -> None:
        """TradeExecutor 通知信号被跳过（未执行交易）。

        将对应的 pending 信号标记为 skipped，后续评估时不计入胜率统计，
        避免未执行信号的理论结果污染 PerformanceTracker 的信号质量反馈。
        """
        if not signal_id:
            return
        with self._lock:
            for pending_list in self._pending.values():
                for p in pending_list:
                    if p.signal_id == signal_id:
                        # 标记为 skipped，_evaluate_pending 会据此跳过
                        p.action = f"skipped:{reason}"
                        return

    def attach(self, runtime: Any) -> None:
        """将 SignalQualityTracker 注册到 SignalRuntime。"""
        runtime.add_signal_listener(self.on_signal_event)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_close_price(self, event: Any) -> Optional[float]:
        """从 SignalEvent 中提取收盘价。

        优先级：
        1. ``event.metadata["close_price"]``：由 SignalRuntime 注入。
        2. 扫描 ``event.indicators`` 所有 payload 的 ``close`` 字段（兜底）。
        """
        raw = event.metadata.get("close_price")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass

        for payload in event.indicators.values():
            if not isinstance(payload, dict):
                continue
            close = payload.get("close")
            if close is not None:
                try:
                    return float(close)
                except (TypeError, ValueError):
                    pass

        return None

    def _record_pending(self, event: Any) -> None:
        """记录一个新的待评估信号。"""
        entry_price = self._get_close_price(event)
        bar_time_raw = event.metadata.get("bar_time", event.generated_at)
        if isinstance(bar_time_raw, str):
            try:
                bar_time = datetime.fromisoformat(bar_time_raw.replace("Z", "+00:00"))
            except ValueError:
                bar_time = datetime.now(timezone.utc)
        elif isinstance(bar_time_raw, datetime):
            bar_time = bar_time_raw
        else:
            bar_time = datetime.now(timezone.utc)
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)

        pending = _PendingSignal(
            signal_id=event.signal_id or "",
            symbol=event.symbol,
            timeframe=event.timeframe,
            strategy=event.strategy,
            action=event.action,
            confidence=event.confidence,
            entry_price=entry_price,
            issued_at=event.generated_at,
            bar_time=bar_time,
            regime=event.metadata.get("regime"),
        )
        key = (event.symbol, event.timeframe, event.strategy)
        with self._lock:
            lst = self._pending.setdefault(key, [])
            lst.append(pending)
            if len(lst) > self._max_pending:
                lst.pop(0)

    def _advance_pending(self, event: Any) -> None:
        """收到新的 confirmed 快照，推进同一 (symbol, timeframe) 下所有策略的待评估计数。

        去重机制：同一根 bar 收盘后，N 个策略各自产生 SignalEvent 并触发此方法，
        但 bars_elapsed 只应推进 1 次（代表"又过了一根 bar"）。
        使用 (symbol, timeframe) → bar_time 映射去重。
        """
        exit_price = self._get_close_price(event)
        if exit_price is None:
            return

        bar_time_raw = event.metadata.get("bar_time")
        symbol_tf = (event.symbol, event.timeframe)
        to_evaluate: List[_PendingSignal] = []

        with self._lock:
            # 同一 (symbol, timeframe) 同一 bar_time 只推进一次
            # bar_time_raw 为 None 时用 fallback 避免多个无 bar_time 事件被误去重
            dedup_key = bar_time_raw if bar_time_raw is not None else f"_fallback_{time.monotonic()}"
            if self._last_advance_bar.get(symbol_tf) == dedup_key:
                return
            self._last_advance_bar[symbol_tf] = dedup_key

            keys_to_scan = [
                k for k in self._pending if k[0] == symbol_tf[0] and k[1] == symbol_tf[1]
            ]
            for key in keys_to_scan:
                lst = self._pending.get(key, [])
                remaining: List[_PendingSignal] = []
                for p in lst:
                    p.bars_elapsed += 1
                    if p.bars_elapsed >= self._bars_to_evaluate:
                        to_evaluate.append(p)
                    else:
                        remaining.append(p)
                self._pending[key] = remaining

        if not to_evaluate:
            return

        rows: List[Tuple] = []
        now = datetime.now(timezone.utc)
        for p in to_evaluate:
            if p.entry_price is not None and exit_price is not None:
                price_change = exit_price - p.entry_price
                if p.action == "buy":
                    won = price_change > 0
                elif p.action == "sell":
                    won = price_change < 0
                else:
                    won = None
            else:
                price_change = None
                won = None

            with self._lock:
                if won is not None:
                    self._total_evaluated += 1
                    if won:
                        self._total_wins += 1

            if won is not None and self._on_quality_fn is not None:
                try:
                    self._on_quality_fn(
                        p.strategy, won, price_change or 0.0,
                        regime=p.regime,
                    )
                except Exception:
                    logger.debug(
                        "SignalQualityTracker: on_quality_fn callback failed for %s",
                        p.strategy, exc_info=True,
                    )

            rows.append((
                now,
                p.signal_id,
                p.symbol,
                p.timeframe,
                p.strategy,
                p.action,
                p.confidence,
                p.entry_price,
                exit_price,
                price_change,
                won,
                p.bars_elapsed,
                p.regime,
                {"entry_bar_time": p.bar_time.isoformat()},
            ))

        try:
            self._write_fn(rows)
        except Exception:
            logger.exception(
                "SignalQualityTracker: failed to write %d outcome rows for %s/%s",
                len(rows), event.symbol, event.timeframe,
            )
