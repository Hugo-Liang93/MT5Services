from __future__ import annotations

SESSION_ASIA = "asia"
SESSION_LONDON = "london"
SESSION_NEW_YORK = "new_york"
SESSION_OFF_HOURS = "off_hours"


def normalize_session_name(name: str) -> str:
    value = str(name).strip().lower()
    legacy_aliases = {
        "newyork": SESSION_NEW_YORK,
    }
    value = legacy_aliases.get(value, value)
    if value in {SESSION_ASIA, SESSION_LONDON, SESSION_NEW_YORK, SESSION_OFF_HOURS}:
        return value
    return value


def resolve_session_by_hour(hour: int | None) -> str:
    if hour is None:
        return "unknown"
    if 0 <= hour < 7:
        return SESSION_ASIA
    if 7 <= hour < 13:
        return SESSION_LONDON
    if 13 <= hour < 21:
        return SESSION_NEW_YORK
    return SESSION_OFF_HOURS
