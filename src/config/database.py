from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel

from src.config.utils import load_config_with_base


class DBSettings(BaseModel):
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_database: str = "mt5"
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
    return DBSettings(
        pg_host=_cfg_str(sec, "host", "localhost"),
        pg_port=_cfg_int(sec, "port", 5432),
        pg_user=_cfg_str(sec, "user", "postgres"),
        pg_password=_cfg_str(sec, "password", "postgres"),
        pg_database=_cfg_str(sec, "database", "mt5"),
        pg_schema=_cfg_str(sec, "schema", "public"),
    )
