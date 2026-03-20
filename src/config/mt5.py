from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import BaseModel

from src.config.utils import load_config_with_base


class MT5Settings(BaseModel):
    account_alias: str = "default"
    account_label: Optional[str] = None
    mt5_login: Optional[int] = None
    mt5_password: Optional[str] = None
    mt5_server: Optional[str] = None
    mt5_path: Optional[str] = None
    timezone: str = "UTC"
    server_time_offset_hours: Optional[int] = None
    enabled: bool = True


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


def _cfg_bool(sec, key: str, default: bool):
    if sec is None:
        return default
    val = sec.get(key, fallback=None)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _build_mt5_settings(base_sec, override_sec=None, *, alias: str = "default") -> MT5Settings:
    def _pick_str(key: str, default=None):
        value = _cfg_str(override_sec, key, None)
        if value not in (None, ""):
            return value
        return _cfg_str(base_sec, key, default)

    def _pick_int(key: str, default=None):
        value = _cfg_int(override_sec, key, None)
        if value is not None:
            return value
        return _cfg_int(base_sec, key, default)

    enabled_value = _cfg_bool(override_sec, "enabled", _cfg_bool(base_sec, "enabled", True))
    return MT5Settings(
        account_alias=alias,
        account_label=_pick_str("label", None),
        mt5_login=_pick_int("login", None),
        mt5_password=_pick_str("password", None),
        mt5_server=_pick_str("server", None),
        mt5_path=_pick_str("path", None),
        timezone=_pick_str("timezone", "UTC"),
        server_time_offset_hours=_pick_int("server_time_offset_hours", None),
        enabled=enabled_value,
    )


def load_mt5_accounts() -> dict[str, MT5Settings]:
    path, parser = load_config_with_base("mt5.ini")
    if not path or not parser:
        return {"default": MT5Settings()}

    base_sec = parser["mt5"] if parser.has_section("mt5") else None
    accounts: dict[str, MT5Settings] = {}
    default_alias = (
        _cfg_str(base_sec, "default_account", "default") if base_sec is not None else "default"
    )

    for section_name in parser.sections():
        if not section_name.startswith("account."):
            continue
        alias = section_name.split(".", 1)[1].strip()
        if not alias:
            continue
        settings = _build_mt5_settings(base_sec, parser[section_name], alias=alias)
        if settings.enabled:
            accounts[alias] = settings

    base_has_profile = any(
        value not in (None, "")
        for value in (
            _cfg_int(base_sec, "login", None),
            _cfg_str(base_sec, "server", None),
            _cfg_str(base_sec, "path", None),
        )
    )
    if base_has_profile or not accounts or default_alias == "default":
        accounts.setdefault("default", _build_mt5_settings(base_sec, None, alias="default"))

    if default_alias in accounts and default_alias != "default":
        ordered = {default_alias: accounts[default_alias]}
        ordered.update(
            {alias: settings for alias, settings in accounts.items() if alias != default_alias}
        )
        return ordered
    return accounts


@lru_cache
def load_mt5_settings(account_alias: Optional[str] = None) -> MT5Settings:
    accounts = load_mt5_accounts()
    if not accounts:
        return MT5Settings()
    if account_alias is None:
        return next(iter(accounts.values()))
    if account_alias not in accounts:
        raise KeyError(f"MT5 account alias not configured: {account_alias}")
    return accounts[account_alias]
