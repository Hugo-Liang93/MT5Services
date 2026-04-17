"""Notification module config loader.

Layered merge order (later overrides earlier):
    1. config/notifications.ini
    2. config/notifications.local.ini
    3. config/instances/<instance>/notifications.ini (if instance-scoped)
    4. config/instances/<instance>/notifications.local.ini (if instance-scoped)

The bot_token and chat_ids must live in *.local.ini (gitignored).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, List

from src.config.models.notifications import NotificationConfig, Severity
from src.config.utils import get_merged_config

logger = logging.getLogger(__name__)

_VALID_SEVERITIES: frozenset[str] = frozenset({"off", "info", "warning", "critical"})


def _split_csv(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _parse_float_list(value: Any) -> List[float]:
    items = _split_csv(value)
    result: List[float] = []
    for item in items:
        try:
            result.append(float(item))
        except ValueError as exc:
            raise ValueError(f"invalid float in list: {item!r}") from exc
    return result


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off", ""}:
        return False
    raise ValueError(f"cannot parse as bool: {value!r}")


def _parse_events(events_section: Dict[str, Any]) -> Dict[str, Severity]:
    result: Dict[str, Severity] = {}
    for raw_key, raw_value in events_section.items():
        key = str(raw_key).strip()
        value = str(raw_value).strip().lower()
        if not key:
            continue
        if value not in _VALID_SEVERITIES:
            raise ValueError(
                f"notifications.ini [events] {key!r}: invalid severity {raw_value!r} "
                f"(expected one of {sorted(_VALID_SEVERITIES)})"
            )
        result[key] = value  # type: ignore[assignment]
    return result


def _build_runtime_section(section: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if "enabled" in section:
        payload["enabled"] = _parse_bool(section["enabled"])
    if "inbound_enabled" in section:
        payload["inbound_enabled"] = _parse_bool(section["inbound_enabled"])
    for key in (
        "poll_interval_seconds",
        "http_timeout_seconds",
    ):
        if key in section and str(section[key]).strip():
            payload[key] = float(section[key])
    for key in (
        "getupdates_timeout_seconds",
        "worker_queue_size",
        "max_retry_attempts",
    ):
        if key in section and str(section[key]).strip():
            payload[key] = int(section[key])
    if "retry_backoff_seconds" in section:
        payload["retry_backoff_seconds"] = _parse_float_list(
            section["retry_backoff_seconds"]
        )
    proxy = str(section.get("http_proxy_url", "")).strip()
    payload["http_proxy_url"] = proxy or None
    return payload


def _build_dedup_section(section: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in ("critical_ttl_seconds", "warning_ttl_seconds", "info_ttl_seconds"):
        if key in section and str(section[key]).strip():
            payload[key] = int(section[key])
    return payload


def _build_rate_limit_section(section: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in (
        "global_per_minute",
        "per_chat_per_minute",
        "inbound_per_chat_per_minute",
    ):
        if key in section and str(section[key]).strip():
            payload[key] = int(section[key])
    return payload


def _build_schedules_section(section: Dict[str, Any]) -> Dict[str, Any]:
    value = str(section.get("daily_report_utc", "")).strip()
    return {"daily_report_utc": value or None}


def _build_templates_section(section: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    directory = str(section.get("directory", "")).strip()
    if directory:
        payload["directory"] = directory
    if "strict_validation" in section:
        payload["strict_validation"] = _parse_bool(
            section["strict_validation"], default=True
        )
    return payload


def _build_inbound_section(section: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if "enabled" in section:
        payload["enabled"] = _parse_bool(section["enabled"])
    payload["command_whitelist"] = _split_csv(section.get("command_whitelist"))
    payload["allowed_chat_ids"] = _split_csv(section.get("allowed_chat_ids"))
    payload["admin_chat_ids"] = _split_csv(section.get("admin_chat_ids"))
    return payload


def _build_event_filters_section(section: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "risk_rejection_reasons": _split_csv(section.get("risk_rejection_reasons")),
        "suppress_info_on_instances": _split_csv(
            section.get("suppress_info_on_instances")
        ),
    }


def _build_chats_section(section: Dict[str, Any]) -> Dict[str, Any]:
    return {"default_chat_id": str(section.get("default_chat_id", "")).strip()}


def _extract_bot_token(merged: Dict[str, Any]) -> str:
    # bot_token 支持放在根 section（ini 兼容写法）或 [runtime]、[secrets] 任意位置
    for section_name in ("runtime", "secrets", "chats"):
        section = merged.get(section_name, {})
        token = str(section.get("bot_token", "")).strip()
        if token:
            return token
    return ""


@lru_cache
def get_notification_config() -> NotificationConfig:
    merged = get_merged_config("notifications.ini")
    runtime_section = dict(merged.get("runtime", {}))
    events_section = dict(merged.get("events", {}))
    event_filters_section = dict(merged.get("event_filters", {}))
    dedup_section = dict(merged.get("dedup", {}))
    rate_limit_section = dict(merged.get("rate_limit", {}))
    schedules_section = dict(merged.get("schedules", {}))
    templates_section = dict(merged.get("templates", {}))
    inbound_section = dict(merged.get("inbound", {}))
    chats_section = dict(merged.get("chats", {}))

    payload: Dict[str, Any] = {
        "runtime": _build_runtime_section(runtime_section),
        "events": _parse_events(events_section),
        "event_filters": _build_event_filters_section(event_filters_section),
        "dedup": _build_dedup_section(dedup_section),
        "rate_limit": _build_rate_limit_section(rate_limit_section),
        "schedules": _build_schedules_section(schedules_section),
        "templates": _build_templates_section(templates_section),
        "inbound": _build_inbound_section(inbound_section),
        "chats": _build_chats_section(chats_section),
        "bot_token": _extract_bot_token(merged),
    }
    config = NotificationConfig(**payload)
    logger.debug(
        "notifications config loaded: enabled=%s inbound=%s events=%d",
        config.runtime.enabled,
        config.runtime.inbound_enabled,
        len(config.events),
    )
    return config


def reset_notification_config_cache() -> None:
    get_notification_config.cache_clear()
