"""信号绩效追踪器（OutcomeTracker）。

## 设计思路

OutcomeTracker 作为 SignalRuntime 的 signal_listener 注册，
消费的是 **SignalEvent**（不是 bar close 事件）。

每当 SignalRuntime 发出 confirmed_buy / confirmed_sell 的 SignalEvent 后，
OutcomeTracker 将其记录在内存中（_record_pending），等待 N 根 bar 后的收盘价到来，
然后判断信号方向是否正确（"赢了"），并将结果写入 signal_outcomes 表。

## 集成方式

OutcomeTracker 作为 SignalRuntime 的信号监听器注册：
    outcome_tracker.attach(runtime)  →  runtime.add_signal_listener(on_signal_event)

每次 scope="confirmed" 的 SignalEvent 到来时：
    1. _advance_pending：推进同一 (symbol, timeframe) 下所有策略的 pending 计数
    2. _record_pending：若为 confirmed_buy/sell，登记新的待评估信号（entry_price 从快照 close 读取）
    3. 当 bars_elapsed >= bars_to_evaluate（默认 3），用最新 close 作为 exit_price 计算胜负，写入 DB

## 两条评估路径

1. **自然到期**：_advance_pending 中 bars_elapsed 达标后用收盘价评估
2. **仓位关闭**：on_position_closed 由 PositionManager 回调，用实际成交价立即评估

## 注意

OutcomeTracker 依赖"N 根 bar 后的价格"，
它监听后续的 confirmed SignalEvent（而非直接使用 runtime 内部状态），
因此对系统延迟的容忍度较高，不影响实时信号路径。

结果会通过 _on_outcome_fn 回调通知 StrategyPerformanceTracker（日内实时反馈），
同时写入 signal_outcomes 表供 ConfidenceCalibrator 定期查询（长期统计校准）。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class _PendingOutcome:
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


class OutcomeTracker:
    """在 N 根 bar 后回填信号的实际盈亏结果。

    参数
    ----
    write_fn:
        接受 List[Tuple] 并写入 DB 的回调函数（通常是 TimescaleWriter.write_outcome_events）。
    bars_to_evaluate:
        发出信号后等待多少根 bar 再判定输赢（默认 3）。
    max_pending:
        内存中最多保留多少个待评估信号（超出时丢弃最旧的）。
    """

    def __init__(
        self,
        write_fn: Callable[[List[Tuple]], None],
        *,
        bars_to_evaluate: int = 3,
        max_pending: int = 500,
        on_outcome_fn: Optional[Callable[..., None]] = None,
    ) -> None:
        self._write_fn = write_fn
        self._bars_to_evaluate = max(1, bars_to_evaluate)
        self._max_pending = max_pending
        # 绩效追踪回调：(strategy, won, pnl, *, regime=) — 由 StrategyPerformanceTracker 注册。
        # 每次信号结果评估完成时调用，提供实时日内反馈（含 regime 维度）。
        self._on_outcome_fn = on_outcome_fn
        # key: (symbol, timeframe, strategy) → list of pending outcomes
        self._pending: Dict[Tuple[str, str, str], List[_PendingOutcome]] = {}
        self._lock = threading.Lock()
        self._total_evaluated: int = 0
        self._total_wins: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_signal_event(self, event: Any) -> None:
        """SignalRuntime 信号监听回调。

        - confirmed_buy / confirmed_sell → 记录待评估信号
        - 后续 confirmed snapshot 到来时 → 回填结果
        """
        signal_state = str(event.metadata.get("signal_state", ""))
        scope = str(event.metadata.get("scope", ""))

        if scope != "confirmed":
            return

        # 每个 confirmed bar-close 事件都代表新的一根 bar 收盘，
        # 无论方向如何都应先推进所有待评估信号的 bars_elapsed 计数。
        self._advance_pending(event)
        if signal_state in ("confirmed_buy", "confirmed_sell"):
            self._record_pending(event)

    def on_position_closed(
        self,
        pos: Any,
        close_price: Optional[float],
    ) -> None:
        """O-1: PositionManager 关仓回调 — 立即评估该仓位对应的 pending outcomes。

        无需等待 N 根 bar，仓位一旦关闭即用实际成交价计算盈亏。

        参数
        ----
        pos:
            ``TrackedPosition`` 实例（含 signal_id、symbol、timeframe、strategy、action）。
        close_price:
            仓位关闭时的实际价格；None 表示无法获取（此时跳过评估）。
        """
        if close_price is None:
            return
        try:
            close_price_f = float(close_price)
        except (TypeError, ValueError):
            return

        signal_id: str = getattr(pos, "signal_id", "") or ""
        symbol: str = getattr(pos, "symbol", "")
        timeframe: str = getattr(pos, "timeframe", "")
        strategy: str = getattr(pos, "strategy", "")
        action: str = getattr(pos, "action", "")
        if not all([signal_id, symbol, strategy]):
            return

        key = (symbol, timeframe, strategy)
        to_evaluate: List[_PendingOutcome] = []
        with self._lock:
            lst = self._pending.get(key, [])
            remaining: List[_PendingOutcome] = []
            for p in lst:
                if p.signal_id == signal_id:
                    to_evaluate.append(p)
                else:
                    remaining.append(p)
            if to_evaluate:
                self._pending[key] = remaining

        if not to_evaluate:
            return

        rows: List[Tuple] = []
        now = datetime.now(timezone.utc)
        for p in to_evaluate:
            if p.entry_price is not None:
                price_change = close_price_f - p.entry_price
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

            # 实时绩效回调：通知 PerformanceTracker（含 regime 维度）
            if won is not None and self._on_outcome_fn is not None:
                try:
                    self._on_outcome_fn(
                        p.strategy, won, price_change or 0.0,
                        regime=p.regime,
                    )
                except Exception:
                    logger.debug(
                        "OutcomeTracker: on_outcome_fn callback failed for %s",
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
                close_price_f,
                price_change,
                won,
                p.bars_elapsed,
                p.regime,
                {"entry_bar_time": p.bar_time.isoformat(), "close_source": "position_closed"},
            ))

        if not rows:
            return
        try:
            self._write_fn(rows)
        except Exception:
            logger.exception(
                "OutcomeTracker.on_position_closed: failed to write %d rows", len(rows)
            )

    def winrate_summary(self) -> Dict[str, Any]:
        """返回内存中统计的实时胜率（不依赖 DB）。"""
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

    def attach(self, runtime: Any) -> None:
        """将 OutcomeTracker 注册到 SignalRuntime。"""
        runtime.add_signal_listener(self.on_signal_event)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_close_price(self, event: Any) -> Optional[float]:
        """从 SignalEvent 中提取收盘价。

        优先级：
        1. ``event.metadata["close_price"]``：由 SignalRuntime 在策略域收窄前，
           从完整指标快照中提取并注入，对所有策略均有效（RSI/MACD/Supertrend 等）。
        2. 扫描 ``event.indicators`` 所有 payload 的 ``close`` 字段（兜底）。
        """
        # Priority 1: runtime-injected close (strategy-agnostic, always from full snapshot)
        raw = event.metadata.get("close_price")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass

        # Priority 2: scan all scoped indicator payloads for a close field
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

        pending = _PendingOutcome(
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
            # 超出上限时丢弃最旧的
            if len(lst) > self._max_pending:
                lst.pop(0)

    def _advance_pending(self, event: Any) -> None:
        """收到新的 confirmed 快照，推进同一 (symbol, timeframe) 下所有策略的待评估计数。

        每一根 bar 的收盘对所有策略是同一个物理事件，因此推进所有策略的 bars_elapsed。
        当 bars_elapsed >= bars_to_evaluate 时，用当前 bar 的 close 作为 exit_price 评估胜负。
        """
        exit_price = self._get_close_price(event)
        if exit_price is None:
            return

        symbol_tf = (event.symbol, event.timeframe)
        to_evaluate: List[_PendingOutcome] = []

        with self._lock:
            # 遍历所有 (symbol, timeframe, *) 的 pending 记录
            keys_to_scan = [
                k for k in self._pending if k[0] == symbol_tf[0] and k[1] == symbol_tf[1]
            ]
            for key in keys_to_scan:
                lst = self._pending.get(key, [])
                remaining: List[_PendingOutcome] = []
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

            # 实时绩效回调：通知 PerformanceTracker（含 regime 维度）
            if won is not None and self._on_outcome_fn is not None:
                try:
                    self._on_outcome_fn(
                        p.strategy, won, price_change or 0.0,
                        regime=p.regime,
                    )
                except Exception:
                    logger.debug(
                        "OutcomeTracker: on_outcome_fn callback failed for %s",
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
                "OutcomeTracker: failed to write %d outcome rows for %s/%s",
                len(rows), event.symbol, event.timeframe,
            )
