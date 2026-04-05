from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel

from src.config.utils import load_config_with_base


class DBSettings(BaseModel):
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = ""
    pg_password: str = ""
    pg_database: str = ""
    pg_schema: str = "public"


def _load_ini_section(filename: str, section: str):
    path, parser = load_config_with_base(filename)
    if not path or not parser:
        return None
    return parser[section] if parser.has_section(section) else None


def _cfg_str(sec, key: str, default=None):
    if sec is None:
        return default
    value = sec.get(key, fallback=default)
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1].strip()
    return normalized


def _cfg_int(sec, key: str, default=None):
    if sec is None:
        return default
    try:
        return sec.getint(key, fallback=default)
    except Exception:
        return default


@lru_cache
def load_db_settings() -> DBSettings:
    sec = _load_ini_section("db.ini", "db")
    settings = DBSettings(
        pg_host=_cfg_str(sec, "host", "localhost"),
        pg_port=_cfg_int(sec, "port", 5432),
        pg_user=_cfg_str(sec, "user", ""),
        pg_password=_cfg_str(sec, "password", ""),
        pg_database=_cfg_str(sec, "database", ""),
        pg_schema=_cfg_str(sec, "schema", "public"),
    )
    missing = []
    if not settings.pg_user:
        missing.append("user")
    if not settings.pg_password:
        missing.append("password")
    if not settings.pg_database:
        missing.append("database")
    if missing:
        import logging

        logging.getLogger(__name__).error(
            "Database credentials missing: %s. "
            "Please configure in config/db.local.ini (gitignored).",
            ", ".join(missing),
        )
    return settings


def load_retention_config() -> tuple[bool, dict[str, int]]:
    """Load retention policy config from db.ini [retention] section.

    Returns:
        (enabled, override_days) where override_days maps table_name → days.
    """
    sec = _load_ini_section("db.ini", "retention")
    if sec is None:
        return True, {}

    enabled_raw = _cfg_str(sec, "enabled", "true")
    enabled = str(enabled_raw).lower() in {"true", "1", "yes"}

    override_days: dict[str, int] = {}
    skip_keys = {"enabled"}
    for key in sec:
        if key in skip_keys:
            continue
        days = _cfg_int(sec, key)
        if days is not None and days > 0:
            override_days[key] = days

    return enabled, override_days
