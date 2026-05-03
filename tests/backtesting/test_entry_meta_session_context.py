from __future__ import annotations

from datetime import datetime, timezone

from src.backtesting.engine.runner import (
    _build_entry_meta_session_context_filter,
    _resolve_entry_meta_session,
)


def test_entry_meta_session_context_resolves_without_strategy_session_filter() -> None:
    session_context = _build_entry_meta_session_context_filter()

    session = _resolve_entry_meta_session(
        session_context,
        datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc),
    )

    assert session == "london"


def test_entry_meta_session_context_downgrades_empty_sessions_to_unknown() -> None:
    class EmptySessionContext:
        def current_sessions(self, at_time: datetime) -> list[str]:
            return []

    session = _resolve_entry_meta_session(
        EmptySessionContext(),
        datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc),
    )

    assert session == "unknown"


def test_entry_meta_session_context_downgrades_invalid_time_to_unknown() -> None:
    session_context = _build_entry_meta_session_context_filter()

    session = _resolve_entry_meta_session(session_context, "invalid-time")

    assert session == "unknown"


def test_entry_meta_session_context_downgrades_missing_time_to_unknown() -> None:
    session_context = _build_entry_meta_session_context_filter()

    session = _resolve_entry_meta_session(session_context, None)

    assert session == "unknown"
