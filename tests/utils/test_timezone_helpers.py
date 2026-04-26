from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.utils.timezone import parse_iso_to_utc, to_utc


# ── §0w R4：parse_iso_to_utc helper 必须保持绝对时刻而非改时区标签 ──


def test_parse_iso_to_utc_converts_aware_input_preserving_absolute_moment() -> None:
    """带偏移 ISO 必须做 astimezone 转换，不能改标签。"""
    result = parse_iso_to_utc("2026-04-26T08:00:00+08:00")
    expected = datetime(2026, 4, 26, 0, 0, 0, tzinfo=timezone.utc)
    assert result == expected, (
        f"+08:00 偏移必须转 UTC 绝对时刻；got {result.isoformat()!r}, "
        f"expected {expected.isoformat()!r}"
    )


def test_parse_iso_to_utc_treats_naive_as_utc() -> None:
    """naive ISO 默认按 UTC 解释（无别的信息）。"""
    result = parse_iso_to_utc("2026-04-26T08:00:00")
    assert result == datetime(2026, 4, 26, 8, 0, 0, tzinfo=timezone.utc)
    assert result.tzinfo is timezone.utc


def test_parse_iso_to_utc_handles_already_utc_input() -> None:
    """已是 UTC 输入直接返回 UTC（绝对时刻不变）。"""
    result = parse_iso_to_utc("2026-04-26T00:00:00+00:00")
    assert result == datetime(2026, 4, 26, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_to_utc_handles_negative_offset() -> None:
    """负偏移（如 EST）也能正确转 UTC。"""
    result = parse_iso_to_utc("2026-04-26T08:00:00-05:00")
    expected = datetime(2026, 4, 26, 13, 0, 0, tzinfo=timezone.utc)
    assert result == expected


def test_to_utc_preserves_aware_absolute_moment() -> None:
    """to_utc 直接拿 datetime 也走 astimezone（不改标签）。"""
    aware = datetime(2026, 4, 26, 8, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    result = to_utc(aware)
    assert result == datetime(2026, 4, 26, 0, 0, 0, tzinfo=timezone.utc)


def test_to_utc_assumes_naive_is_utc() -> None:
    naive = datetime(2026, 4, 26, 8, 0, 0)
    assert to_utc(naive) == datetime(2026, 4, 26, 8, 0, 0, tzinfo=timezone.utc)
