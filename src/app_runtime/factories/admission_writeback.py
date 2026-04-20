"""Signal admission writeback listeners (P9 Phase 1.5).

把 executor admission 决策（accept / reject）回填到 signal_events 表。
listener 内部自带 try/except 异常隔离——回填失败不影响执行链。

两条路径：
- ``make_skip_listener``  → 监听 ``TradeExecutor.on_execution_skip`` 回调
                             → actionability="blocked"
- ``make_intent_published_listener`` → 订阅 ``PipelineEventBus`` 的
                             ``PIPELINE_INTENT_PUBLISHED`` 事件 → actionability="actionable"

paper_only / candidate_only 等未到 executor 的 deployment 兜底由
``SignalRuntime`` 在 deployment 路由判断点直接回填（待后续接入）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def make_skip_listener(signal_repo: Any) -> Callable[[str, str], None]:
    """构造拒单 listener。

    返回的 callback 与 ``TradeExecutor.on_execution_skip`` 签名一致：
    ``(signal_id: str, reason: str) -> None``。
    """

    def on_skip(signal_id: str, reason: str) -> None:
        if not signal_id:
            return
        try:
            signal_repo.update_admission_result(
                signal_id=signal_id,
                actionability="blocked",
                guard_reason_code=reason or None,
            )
        except Exception:
            logger.debug(
                "signal admission writeback (skip) failed for signal_id=%s",
                signal_id,
                exc_info=True,
            )

    return on_skip


def make_intent_published_listener(signal_repo: Any) -> Callable[[Any], None]:
    """构造 PipelineEventBus listener。

    PipelineEventBus.add_listener 注册的 listener 会收到**全部**事件，
    本 listener 按 ``event.type == "intent_published"`` 自行过滤。
    PipelineEvent.payload 必含 ``signal_id``（参见 publisher.py:181）。
    """
    from src.monitoring.pipeline.events import PIPELINE_INTENT_PUBLISHED

    def on_event(event: Any) -> None:
        if getattr(event, "type", None) != PIPELINE_INTENT_PUBLISHED:
            return
        payload = getattr(event, "payload", None) or {}
        signal_id = payload.get("signal_id") if isinstance(payload, dict) else None
        if not signal_id:
            return
        try:
            signal_repo.update_admission_result(
                signal_id=str(signal_id),
                actionability="actionable",
                guard_reason_code=None,
            )
        except Exception:
            logger.debug(
                "signal admission writeback (accept) failed for signal_id=%s",
                signal_id,
                exc_info=True,
            )

    return on_event


def multicast(*callbacks: Callable[..., None]) -> Callable[..., None]:
    """Fan-out adapter：多个 callback 串行调用，单个失败不影响其他。

    用于 ``on_execution_skip`` 同时驱动 SignalQualityTracker 统计 + admission writeback。
    """
    callbacks_filtered = tuple(cb for cb in callbacks if callable(cb))

    def fan_out(*args: Any, **kwargs: Any) -> None:
        for cb in callbacks_filtered:
            try:
                cb(*args, **kwargs)
            except Exception:
                logger.debug(
                    "multicast callback %s raised; isolated",
                    getattr(cb, "__name__", repr(cb)),
                    exc_info=True,
                )

    return fan_out
