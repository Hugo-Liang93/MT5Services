from __future__ import annotations

from pathlib import Path

import src.config.instance_context as instance_context
from src.config.database import load_db_settings
from src.config.mt5 import load_group_mt5_settings, load_mt5_settings
from src.config.runtime_identity import (
    build_account_key,
    get_runtime_identity,
    validate_mt5_topology,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_load_group_mt5_settings_aggregates_instance_accounts(tmp_path) -> None:
    _write(
        tmp_path / "config" / "topology.ini",
        """
[group.live]
main = live-main
workers = live-exec-a
""".strip(),
    )
    _write(
        tmp_path / "config" / "instances" / "live-main" / "mt5.ini",
        """
[mt5]
account_alias = live_main
label = Live Main
""".strip(),
    )
    _write(
        tmp_path / "config" / "instances" / "live-main" / "mt5.local.ini",
        """
[mt5]
login = 1001
server = Broker-Live
path = C:/MT5/main/terminal64.exe
""".strip(),
    )
    _write(
        tmp_path / "config" / "instances" / "live-exec-a" / "mt5.ini",
        """
[mt5]
account_alias = live_exec_a
label = Live Exec A
""".strip(),
    )
    _write(
        tmp_path / "config" / "instances" / "live-exec-a" / "mt5.local.ini",
        """
[mt5]
login = 1002
server = Broker-Live
path = C:/MT5/exec_a/terminal64.exe
""".strip(),
    )

    load_mt5_settings.cache_clear()
    load_group_mt5_settings.cache_clear()
    try:
        accounts = load_group_mt5_settings(
            instance_name="live-main",
            base_dir=str(tmp_path),
        )
    finally:
        load_mt5_settings.cache_clear()
        load_group_mt5_settings.cache_clear()

    assert sorted(accounts.keys()) == ["live_exec_a", "live_main"]
    assert accounts["live_main"].mt5_login == 1001
    assert accounts["live_exec_a"].mt5_path == "C:/MT5/exec_a/terminal64.exe"


def test_load_db_settings_routes_by_environment(monkeypatch):
    import configparser

    parser = configparser.ConfigParser()
    parser.read_dict(
        {
            "db.live": {
                "host": "live-host",
                "port": "5433",
                "user": "live-user",
                "password": "live-pass",
                "database": "live-db",
                "schema": "live",
            },
            "db.demo": {
                "host": "demo-host",
                "port": "5434",
                "user": "demo-user",
                "password": "demo-pass",
                "database": "demo-db",
                "schema": "demo",
            },
        }
    )
    monkeypatch.setattr(
        "src.config.database.load_config_with_base",
        lambda filename: ("config/db.ini", parser),
    )
    monkeypatch.setattr(
        "src.config.database.resolve_current_environment",
        lambda environment=None: "demo" if environment is None else environment,
    )
    load_db_settings.cache_clear()
    try:
        live = load_db_settings("live")
        demo = load_db_settings()
    finally:
        load_db_settings.cache_clear()

    assert live.pg_host == "live-host"
    assert live.pg_database == "live-db"
    assert demo.pg_host == "demo-host"
    assert demo.pg_schema == "demo"


def test_validate_mt5_topology_requires_unique_live_terminal_paths(monkeypatch):
    monkeypatch.setattr(
        "src.config.runtime_identity.resolve_topology_assignment",
        lambda instance_name=None: type(
            "Assignment",
            (),
            {
                "instance_name": "live-main",
                "environment": "live",
                "role": "main",
                "live_topology_mode": "multi_account",
            },
        )(),
    )
    monkeypatch.setattr(
        "src.config.runtime_identity.load_group_mt5_settings",
        lambda **kwargs: {
            "live_main": type(
                "Account",
                (),
                {
                    "account_alias": "live_main",
                    "mt5_path": "C:/MT5/shared/terminal64.exe",
                },
            )(),
            "live_exec_a": type(
                "Account",
                (),
                {
                    "account_alias": "live_exec_a",
                    "mt5_path": "C:/MT5/shared/terminal64.exe",
                },
            )(),
        },
    )
    instance_context.set_current_instance_name("live-main")
    get_runtime_identity.cache_clear()
    try:
        try:
            validate_mt5_topology()
            assert False, "expected duplicate live terminal path validation failure"
        except ValueError as exc:
            assert "terminal path" in str(exc)
    finally:
        instance_context.set_current_instance_name(None)
        get_runtime_identity.cache_clear()


def test_get_runtime_identity_builds_account_key_from_instance_topology(monkeypatch):
    class _Account:
        instance_name = "live-main"
        account_alias = "live_main"
        account_label = "Live Main"
        mt5_server = "Broker-Live"
        mt5_login = 1001
        mt5_path = "C:/MT5/live/terminal64.exe"

    monkeypatch.setattr(
        "src.config.runtime_identity.load_mt5_settings",
        lambda: _Account(),
    )
    monkeypatch.setattr(
        "src.config.runtime_identity.validate_mt5_topology",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.config.runtime_identity.resolve_topology_assignment",
        lambda instance_name=None: type(
            "Assignment",
            (),
            {
                "instance_name": "live-main",
                "environment": "live",
                "role": "main",
                "live_topology_mode": "single_account",
            },
        )(),
    )
    get_runtime_identity.cache_clear()
    try:
        identity = get_runtime_identity()
    finally:
        get_runtime_identity.cache_clear()

    assert identity.instance_name == "live-main"
    assert identity.environment == "live"
    assert identity.account_key == build_account_key("live", "Broker-Live", 1001)
    assert identity.instance_role == "main"
    assert identity.live_topology_mode == "single_account"
