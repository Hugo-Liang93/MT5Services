from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

Severity = Literal["off", "info", "warning", "critical"]


class NotificationRuntimeSection(BaseModel):
    enabled: bool = False
    inbound_enabled: bool = False
    poll_interval_seconds: float = 1.0
    getupdates_timeout_seconds: int = 30
    worker_queue_size: int = 1000
    max_retry_attempts: int = 3
    retry_backoff_seconds: List[float] = Field(
        default_factory=lambda: [5.0, 15.0, 45.0]
    )
    http_proxy_url: str | None = None
    http_timeout_seconds: float = 10.0

    @field_validator("poll_interval_seconds", "http_timeout_seconds")
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be > 0")
        return value

    @field_validator(
        "worker_queue_size", "max_retry_attempts", "getupdates_timeout_seconds"
    )
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be > 0")
        return value

    @field_validator("retry_backoff_seconds")
    @classmethod
    def _backoff_monotonic(cls, value: List[float]) -> List[float]:
        if not value:
            raise ValueError("retry_backoff_seconds cannot be empty")
        if any(v <= 0 for v in value):
            raise ValueError("all backoff values must be > 0")
        return value


class NotificationDedupSection(BaseModel):
    critical_ttl_seconds: int = 300
    warning_ttl_seconds: int = 600
    info_ttl_seconds: int = 1800

    @field_validator("critical_ttl_seconds", "warning_ttl_seconds", "info_ttl_seconds")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("ttl must be >= 0")
        return value


class NotificationRateLimitSection(BaseModel):
    global_per_minute: int = 30
    per_chat_per_minute: int = 10
    inbound_per_chat_per_minute: int = 10

    @field_validator(
        "global_per_minute", "per_chat_per_minute", "inbound_per_chat_per_minute"
    )
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("rate limit must be > 0")
        return value


class NotificationSchedulesSection(BaseModel):
    daily_report_utc: str | None = None

    @field_validator("daily_report_utc")
    @classmethod
    def _valid_hhmm(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        normalized = value.strip().strip('"').strip("'")
        parts = normalized.split(":")
        if len(parts) != 2:
            raise ValueError("daily_report_utc must be HH:MM")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ValueError("daily_report_utc must be HH:MM with integers") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("daily_report_utc out of range")
        return f"{hour:02d}:{minute:02d}"


class NotificationTemplatesSection(BaseModel):
    directory: str = "config/notifications/templates"
    strict_validation: bool = True


class NotificationInboundSection(BaseModel):
    enabled: bool = False
    command_whitelist: List[str] = Field(default_factory=list)
    allowed_chat_ids: List[str] = Field(default_factory=list)
    admin_chat_ids: List[str] = Field(default_factory=list)

    @field_validator("command_whitelist", "allowed_chat_ids", "admin_chat_ids")
    @classmethod
    def _strip_entries(cls, value: List[str]) -> List[str]:
        return [str(v).strip() for v in value if str(v).strip()]

    @model_validator(mode="after")
    def _admin_subset_of_allowed(self) -> "NotificationInboundSection":
        if not self.enabled:
            return self
        allowed = set(self.allowed_chat_ids)
        admin = set(self.admin_chat_ids)
        if not allowed:
            raise ValueError("inbound.enabled=true requires non-empty allowed_chat_ids")
        unknown = admin - allowed
        if unknown:
            raise ValueError(
                f"admin_chat_ids must be subset of allowed_chat_ids (unknown: {sorted(unknown)})"
            )
        return self


class NotificationEventFiltersSection(BaseModel):
    risk_rejection_reasons: List[str] = Field(default_factory=list)
    suppress_info_on_instances: List[str] = Field(default_factory=list)

    @field_validator("risk_rejection_reasons", "suppress_info_on_instances")
    @classmethod
    def _strip_entries(cls, value: List[str]) -> List[str]:
        return [str(v).strip() for v in value if str(v).strip()]


class NotificationChatsSection(BaseModel):
    default_chat_id: str = ""

    @field_validator("default_chat_id")
    @classmethod
    def _strip(cls, value: str) -> str:
        return str(value or "").strip()


class NotificationConfig(BaseModel):
    runtime: NotificationRuntimeSection = Field(
        default_factory=NotificationRuntimeSection
    )
    events: Dict[str, Severity] = Field(default_factory=dict)
    event_filters: NotificationEventFiltersSection = Field(
        default_factory=NotificationEventFiltersSection
    )
    dedup: NotificationDedupSection = Field(default_factory=NotificationDedupSection)
    rate_limit: NotificationRateLimitSection = Field(
        default_factory=NotificationRateLimitSection
    )
    schedules: NotificationSchedulesSection = Field(
        default_factory=NotificationSchedulesSection
    )
    templates: NotificationTemplatesSection = Field(
        default_factory=NotificationTemplatesSection
    )
    inbound: NotificationInboundSection = Field(
        default_factory=NotificationInboundSection
    )
    chats: NotificationChatsSection = Field(default_factory=NotificationChatsSection)
    bot_token: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def _enabled_requires_credentials(self) -> "NotificationConfig":
        if not self.runtime.enabled:
            return self
        if not self.bot_token.get_secret_value().strip():
            raise ValueError("runtime.enabled=true requires bot_token")
        if not self.chats.default_chat_id:
            raise ValueError("runtime.enabled=true requires chats.default_chat_id")
        return self

    def is_event_active(self, event_type: str) -> bool:
        severity = self.events.get(event_type, "off")
        return severity != "off"

    def severity_for(self, event_type: str) -> Severity:
        return self.events.get(event_type, "off")

    def dedup_ttl(self, severity: Severity) -> int:
        if severity == "critical":
            return self.dedup.critical_ttl_seconds
        if severity == "warning":
            return self.dedup.warning_ttl_seconds
        if severity == "info":
            return self.dedup.info_ttl_seconds
        return 0
