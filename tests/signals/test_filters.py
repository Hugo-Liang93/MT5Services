from __future__ import annotations

from datetime import datetime, timezone

from src.signals.filters import SessionFilter


def test_session_filter_normalizes_newyork_alias() -> None:
    try:
        SessionFilter(allowed_sessions=("london", "newyork"))
        assert False, "expected unsupported session names error"
    except ValueError as exc:
        assert "unsupported session names" in str(exc)


def test_session_filter_emits_new_york_session_name() -> None:
    f = SessionFilter()
    sessions = f.current_sessions(datetime(2026, 3, 19, 14, 0, tzinfo=timezone.utc))
    assert "new_york" in sessions
