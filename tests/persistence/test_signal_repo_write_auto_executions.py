from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from src.persistence.repositories.signal_repo import SignalEventRepository


# ── §0w P3 回归：write_auto_executions 不能改 ISO 偏移标签（应做时区换算） ──


class _FakeWriter:
    def __init__(self) -> None:
        self.batches: list[tuple] = []

    def _batch(self, sql: str, batch: list[tuple], page_size: int = 200) -> None:
        self.batches.extend(batch)

    def _json(self, value: Any) -> Any:
        return value


def test_write_auto_executions_converts_aware_iso_to_utc_absolute_moment() -> None:
    """P3 §0w 回归：旧实现 fromisoformat().replace(tzinfo=UTC) 对带偏移的时间串
    直接改标签而非做时区换算 → 2026-04-26T08:00:00+08:00 被写成
    2026-04-26T08:00:00+00:00（错），正确应是 2026-04-26T00:00:00+00:00。
    会扭曲 auto execution 的排序、trace 时间线和基于时间的去重。
    """
    writer = _FakeWriter()
    repo = SignalEventRepository(writer)

    repo.write_auto_executions(
        rows=[
            {
                "at": "2026-04-26T08:00:00+08:00",  # 北京时间
                "signal_id": "sig-1",
                "symbol": "XAUUSD",
                "direction": "buy",
                "strategy": "trendline",
                "success": True,
                "params": {},
            }
        ]
    )

    assert writer.batches, "至少要写入一条"
    executed_at = writer.batches[0][0]
    expected = datetime(2026, 4, 26, 0, 0, 0, tzinfo=timezone.utc)
    assert executed_at == expected, (
        f"+08:00 偏移必须转换为对应 UTC 绝对时刻；"
        f"got {executed_at.isoformat()!r}, expected {expected.isoformat()!r}"
    )


def test_write_auto_executions_preserves_naive_input_as_utc() -> None:
    """对称契约：naive 输入按 UTC 解释（无偏移信息可参考），保持兼容。"""
    writer = _FakeWriter()
    repo = SignalEventRepository(writer)

    repo.write_auto_executions(
        rows=[
            {
                "at": "2026-04-26T08:00:00",  # naive
                "signal_id": "sig-1",
                "symbol": "XAUUSD",
                "direction": "buy",
                "strategy": "trendline",
                "success": True,
                "params": {},
            }
        ]
    )

    executed_at = writer.batches[0][0]
    assert executed_at == datetime(2026, 4, 26, 8, 0, 0, tzinfo=timezone.utc), (
        f"naive 默认按 UTC 解释；got {executed_at.isoformat()!r}"
    )


def test_write_auto_executions_preserves_already_utc_input() -> None:
    """对称契约：已是 UTC 的 ISO 直接写入不变。"""
    writer = _FakeWriter()
    repo = SignalEventRepository(writer)

    repo.write_auto_executions(
        rows=[
            {
                "at": "2026-04-26T00:00:00+00:00",
                "signal_id": "sig-1",
                "symbol": "XAUUSD",
                "direction": "buy",
                "strategy": "trendline",
                "success": True,
                "params": {},
            }
        ]
    )

    executed_at = writer.batches[0][0]
    assert executed_at == datetime(2026, 4, 26, 0, 0, 0, tzinfo=timezone.utc)
