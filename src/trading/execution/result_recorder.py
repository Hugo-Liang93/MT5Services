"""执行结果记录组件。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .reasons import REASON_QUEUE_OVERFLOW

if TYPE_CHECKING:
    from src.signals.models import SignalEvent

    from .executor import TradeExecutor


class ExecutionResultRecorder:
    """集中管理执行链路中的状态统计与队列丢弃记录。"""

    def __init__(self, executor: "TradeExecutor") -> None:
        self._executor = executor

    def record_overflow(
        self,
        event: "SignalEvent",
        *,
        reason: str = REASON_QUEUE_OVERFLOW,
        max_drops: int = 1,
    ) -> None:
        self._executor.execution_quality["queue_overflows"] += 1
        sig_id = getattr(event, "signal_id", "") or ""
        dropped_entry = {
            "signal_id": sig_id,
            "symbol": event.symbol,
            "timeframe": event.timeframe,
            "strategy": event.strategy,
            "direction": getattr(event, "direction", ""),
            "confidence": getattr(event, "confidence", 0.0),
            "dropped_at": time.time(),
        }
        if max_drops > 0:
            self._executor.dropped_signals.append(dropped_entry)
            if len(self._executor.dropped_signals) > self._executor.max_dropped_history:
                self._executor.dropped_signals = self._executor.dropped_signals[
                    -self._executor.max_dropped_history :
                ]
        if self._executor.persist_execution_fn is not None:
            try:
                self._executor.persist_execution_fn(
                    [
                        {
                            "at": datetime.now(timezone.utc).isoformat(),
                            "signal_id": sig_id,
                            "symbol": event.symbol,
                            "timeframe": event.timeframe,
                            "direction": getattr(event, "direction", ""),
                            "strategy": event.strategy,
                            "confidence": getattr(event, "confidence", 0.0),
                            "success": False,
                            "dropped": True,
                            "reason": reason,
                        }
                    ]
                )
            except Exception:
                # 持久化失败不阻塞执行链路；仅保留日志告警。
                pass
