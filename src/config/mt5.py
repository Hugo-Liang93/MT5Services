from __future__ import annotations

import configparser
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from src.config.instance_context import (
    get_current_instance_name,
    normalize_instance_name,
    resolve_instance_config_dir,
)
from src.config.topology import (
    find_topology_group_for_instance,
    iter_group_instances,
)


class MT5Settings(BaseModel):
    instance_name: Optional[str] = None
    account_alias: str = "default"
    account_label: Optional[str] = None
    mt5_login: Optional[int] = None
    mt5_password: Optional[str] = None
    mt5_server: Optional[str] = None
    mt5_path: Optional[str] = None
    timezone: str = "UTC"
    server_time_offset_hours: Optional[int] = None
    enabled: bool = True


_ROOT_DEFAULT_KEYS = {
    "timezone",
    "server_time_offset_hours",
}


def _project_root(base_dir: Optional[str] = None) -> Path:
    return Path(base_dir or Path(__file__).resolve().parents[2])


def _config_root(base_dir: Optional[str] = None) -> Path:
    return _project_root(base_dir) / "config"


def _local_name(filename: str) -> str:
    return filename[:-4] + ".local.ini" if filename.endswith(".ini") else filename + ".local"


def _read_optional_ini(path: Path) -> configparser.ConfigParser | None:
    if not path.exists():
        return None
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def _merge_sections(
    *sections: configparser.SectionProxy | None,
) -> configparser.ConfigParser | None:
    merged = configparser.ConfigParser(interpolation=None)
    merged_any = False
    for section in sections:
        if section is None:
            continue
        merged_any = True
        if not merged.has_section("mt5"):
            merged.add_section("mt5")
        for key, value in section.items():
            merged.set("mt5", key, value)
    return merged if merged_any else None


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
    value = sec.get(key, fallback=None)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_mt5_layer(
    filename: str,
    *,
    base_dir: Optional[str] = None,
    instance_name: Optional[str] = None,
) -> configparser.SectionProxy | None:
    normalized_instance = normalize_instance_name(get_current_instance_name(instance_name))
    if normalized_instance is None:
        parser = _read_optional_ini(_config_root(base_dir) / filename)
        if parser is None or not parser.has_section("mt5"):
            return None
        return parser["mt5"]

    instance_dir = resolve_instance_config_dir(
        instance_name=normalized_instance,
        base_dir=str(_project_root(base_dir)),
    )
    if instance_dir is None:
        return None
    parser = _read_optional_ini(instance_dir / filename)
    if parser is None or not parser.has_section("mt5"):
        return None
    return parser["mt5"]


def _load_mt5_sections(
    *,
    instance_name: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> tuple[configparser.SectionProxy | None, configparser.SectionProxy | None]:
    root_section = _merge_sections(
        _load_mt5_layer("mt5.ini", base_dir=base_dir),
        _load_mt5_layer(_local_name("mt5.ini"), base_dir=base_dir),
    )
    root_sec = root_section["mt5"] if root_section and root_section.has_section("mt5") else None

    normalized_instance = normalize_instance_name(get_current_instance_name(instance_name))
    if normalized_instance is None:
        return root_sec, None

    instance_section = _merge_sections(
        _load_mt5_layer("mt5.ini", base_dir=base_dir, instance_name=normalized_instance),
        _load_mt5_layer(_local_name("mt5.ini"), base_dir=base_dir, instance_name=normalized_instance),
    )
    instance_sec = (
        instance_section["mt5"]
        if instance_section and instance_section.has_section("mt5")
        else None
    )
    return root_sec, instance_sec


def _pick_value(
    root_sec: configparser.SectionProxy | None,
    instance_sec: configparser.SectionProxy | None,
    key: str,
    *,
    default=None,
):
    if instance_sec is not None:
        value = _cfg_str(instance_sec, key, None)
        if value not in (None, ""):
            return value
        if key in _ROOT_DEFAULT_KEYS:
            return _cfg_str(root_sec, key, default)
        return default
    return _cfg_str(root_sec, key, default)


def _pick_int(
    root_sec: configparser.SectionProxy | None,
    instance_sec: configparser.SectionProxy | None,
    key: str,
    *,
    default=None,
):
    if instance_sec is not None:
        value = _cfg_int(instance_sec, key, None)
        if value is not None:
            return value
        if key in _ROOT_DEFAULT_KEYS:
            return _cfg_int(root_sec, key, default)
        return default
    return _cfg_int(root_sec, key, default)


def _pick_bool(
    root_sec: configparser.SectionProxy | None,
    instance_sec: configparser.SectionProxy | None,
    key: str,
    *,
    default: bool,
) -> bool:
    if instance_sec is not None:
        if instance_sec.get(key, fallback=None) is not None:
            return _cfg_bool(instance_sec, key, default)
        if key in _ROOT_DEFAULT_KEYS:
            return _cfg_bool(root_sec, key, default)
        return default
    return _cfg_bool(root_sec, key, default)


@lru_cache
def load_mt5_settings(
    *,
    instance_name: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> MT5Settings:
    normalized_instance = normalize_instance_name(get_current_instance_name(instance_name))
    root_sec, instance_sec = _load_mt5_sections(
        instance_name=normalized_instance,
        base_dir=base_dir,
    )

    alias_default = normalized_instance or "default"
    account_alias = _pick_value(
        root_sec,
        instance_sec,
        "account_alias",
        default=alias_default,
    )
    account_label = _pick_value(
        root_sec,
        instance_sec,
        "label",
        default=account_alias,
    )

    return MT5Settings(
        instance_name=normalized_instance,
        account_alias=str(account_alias or alias_default).strip() or alias_default,
        account_label=str(account_label).strip() if account_label not in (None, "") else None,
        mt5_login=_pick_int(root_sec, instance_sec, "login", default=None),
        mt5_password=_pick_value(root_sec, instance_sec, "password", default=None),
        mt5_server=_pick_value(root_sec, instance_sec, "server", default=None),
        mt5_path=_pick_value(root_sec, instance_sec, "path", default=None),
        timezone=_pick_value(root_sec, instance_sec, "timezone", default="UTC"),
        server_time_offset_hours=_pick_int(
            root_sec,
            instance_sec,
            "server_time_offset_hours",
            default=None,
        ),
        enabled=_pick_bool(root_sec, instance_sec, "enabled", default=True),
    )


@lru_cache
def load_group_mt5_settings(
    *,
    group_name: Optional[str] = None,
    instance_name: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> dict[str, MT5Settings]:
    normalized_instance = normalize_instance_name(get_current_instance_name(instance_name))
    resolved_group = group_name
    if resolved_group is None:
        group = find_topology_group_for_instance(normalized_instance, base_dir=base_dir)
        if group is None:
            settings = load_mt5_settings(instance_name=normalized_instance, base_dir=base_dir)
            return {settings.account_alias: settings}
        resolved_group = group.name

    accounts: dict[str, MT5Settings] = {}
    for group_instance in iter_group_instances(resolved_group, base_dir=base_dir):
        settings = load_mt5_settings(instance_name=group_instance, base_dir=base_dir)
        alias = settings.account_alias
        if alias in accounts and accounts[alias].instance_name != settings.instance_name:
            raise ValueError(
                f"duplicate MT5 account_alias configured across instances: {alias}"
            )
        accounts[alias] = settings
    return accounts
