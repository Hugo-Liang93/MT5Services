"""Regression tests for src/signals/orchestration/runtime_recovery.py.

P2 历史 bug：_restore_state 内层 for-row 循环对 generated_at / bar_time 调用
runtime._parse_event_time()，但循环没有 per-row 异常隔离。库里混入一条
坏时间串，ValueError 直接冒出 → 整个 restore 中断 → 后续 rows 无法恢复。

prepare_startup() 无保护地调 restore_state()，所以脏数据让整个 startup 挂掉。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from src.signals.orchestration.runtime_recovery import restore_state


def _stub_runtime(rows: list[dict[str, Any]]) -> Any:
    runtime = MagicMock()
    runtime._targets = []
    runtime._state_by_target = {}
    runtime.service.recent_signals.return_value = rows

    # 让 _parse_event_time 真实抛 ValueError 复现脏数据场景
    from datetime import datetime

    def _parse(value: Any) -> datetime:
        if value == "bad-date":
            raise ValueError("Invalid isoformat string: 'bad-date'")
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    runtime._parse_event_time = _parse
    return runtime


def test_restore_state_isolates_per_row_bad_time_string() -> None:
    """P2 回归：单条坏 generated_at 不应中断整个 restore。

    旧实现 row 循环无 try/except，runtime._parse_event_time 抛 ValueError
    直接冒出 → SignalRuntime.start() 经由 prepare_startup → restore_state
    被打挂 → 脏数据让整个启动恢复挂掉。
    """
    rows = [
        # 坏行：bad-date
        {
            "symbol": "XAUUSD",
            "timeframe": "M30",
            "strategy": "trend",
            "generated_at": "bad-date",
            "metadata": {"bar_time": "bad-date"},
        },
        # 好行：应仍能 restore
        {
            "symbol": "XAUUSD",
            "timeframe": "H1",
            "strategy": "breakout",
            "generated_at": "2026-04-26T10:00:00+00:00",
            "metadata": {"bar_time": "2026-04-26T10:00:00+00:00"},
        },
    ]
    runtime = _stub_runtime(rows)

    # 必须不抛
    restore_state(runtime)


def test_restore_state_continues_after_single_bad_row() -> None:
    """坏行之后的好行仍要 restore（非 fail-fast 整批退出）。"""
    rows = [
        {
            "symbol": "XAUUSD",
            "timeframe": "M30",
            "strategy": "bad_strategy",
            "generated_at": "bad-date",
            "metadata": {"bar_time": "bad-date"},
        },
        {
            "symbol": "XAUUSD",
            "timeframe": "H1",
            "strategy": "good_strategy",
            "generated_at": "2026-04-26T10:00:00+00:00",
            "metadata": {"bar_time": "2026-04-26T10:00:00+00:00"},
        },
    ]
    runtime = _stub_runtime(rows)
    restore_state(runtime)
    # good row 进了 _state_by_target
    assert ("XAUUSD", "H1", "good_strategy") in runtime._state_by_target, (
        "坏行隔离后，后续好行必须仍能 restore；当前 keys: "
        f"{list(runtime._state_by_target.keys())!r}"
    )
