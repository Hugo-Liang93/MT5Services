from __future__ import annotations

from src.signals.contracts import normalize_session_name, resolve_session_by_hour


def test_normalize_session_name_accepts_legacy_alias() -> None:
    assert normalize_session_name("newyork") == "new_york"
    assert normalize_session_name("new_york") == "new_york"


def test_resolve_session_by_hour() -> None:
    assert resolve_session_by_hour(2) == "asia"
    assert resolve_session_by_hour(9) == "london"
    assert resolve_session_by_hour(15) == "new_york"
    assert resolve_session_by_hour(22) == "off_hours"
    assert resolve_session_by_hour(None) == "unknown"
