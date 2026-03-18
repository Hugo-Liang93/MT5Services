"""信号绩效追踪器（OutcomeTracker）。

## 设计思路

每当 SignalRuntime 发出 confirmed_buy / confirmed_sell 信号后，
OutcomeTracker 将其记录在内存中，等待 N 根 bar 后的收盘价到来，
然后判断信号方向是否正确（"赢了"），并将结果写入 signal_outcomes 表。

## 集成方式

OutcomeTracker 作为 SignalRuntime 的信号监听器注册：
    outcome_tracker.attach(runtime)

每次 confirmed 信号到来时：
    1. 记录 entry_price（从 indicators 快照中读取 close）
    2. 在 bars_to_evaluate（默认 3）根 bar 后，读取 exit_price
    3. 计算 price_change，判断 won，写入 DB

## 注意

由于 OutcomeTracker 依赖"N 根 bar 后的价格"，
它监听后续的 confirmed snapshot 事件（而非直接使用 runtime 内部状态），
因此对系统延迟的容忍度较高，不影响实时信号路径。
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
    ) -> None:
        self._write_fn = write_fn
        self._bars_to_evaluate = max(1, bars_to_evaluate)
        self._max_pending = max_pending
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
        # 修复前：confirmed_buy/sell 仅调用 _record_pending，_tick_pending 被跳过，
        # 导致趋势行情中 pending 条目无限堆积，胜率统计和置信度校准永远不触发。
        self._tick_pending(event)
        if signal_state in ("confirmed_buy", "confirmed_sell"):
            self._record_pending(event)

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
        """从 SignalEvent.indicators 中提取收盘价。"""
        for ind_name in ("boll20", "sma20", "ema50", "ema200", "atr14"):
            payload = event.indicators.get(ind_name)
            if isinstance(payload, dict):
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

    def _tick_pending(self, event: Any) -> None:
        """收到新的 confirmed 快照，推进所有待评估信号的计数。"""
        exit_price = self._get_close_price(event)
        if exit_price is None:
            return

        key = (event.symbol, event.timeframe, event.strategy)
        to_evaluate: List[_PendingOutcome] = []

        with self._lock:
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
