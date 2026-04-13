from __future__ import annotations

from pathlib import Path

from src.config.instance_context import resolve_instance_scoped_dir, set_current_instance_name
from src.config.mt5 import load_mt5_settings
from src.config.topology import (
    load_topology_group,
    resolve_topology_assignment,
)
from src.config.utils import get_merged_option_source, load_config_with_base


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_load_config_with_base_applies_instance_market_overrides(tmp_path) -> None:
    _write(
        tmp_path / "config" / "app.ini",
        """
[system]
api_host = 0.0.0.0
api_port = 8808
""".strip(),
    )
    _write(
        tmp_path / "config" / "market.ini",
        """
[api]
host = 0.0.0.0
port = 8808
""".strip(),
    )
    _write(
        tmp_path / "config" / "instances" / "live-main" / "market.ini",
        """
[api]
port = 9901
""".strip(),
    )

    path, parser = load_config_with_base(
        "market.ini",
        base_dir=str(tmp_path),
        instance_name="live-main",
    )

    assert path is not None
    assert parser is not None
    assert parser["api"]["host"] == "0.0.0.0"
    assert parser["api"]["port"] == "9901"
    assert (
        get_merged_option_source(
            "market.ini",
            "api",
            "port",
            base_dir=str(tmp_path),
            instance_name="live-main",
        )
        == "instances/live-main/market.ini[api].port"
    )


def test_load_topology_group_and_assignment_reads_group_instances(tmp_path) -> None:
    _write(
        tmp_path / "config" / "topology.ini",
        """
[group.live]
main = live-main
workers = live-exec-a, live-exec-b
""".strip(),
    )

    group = load_topology_group("live", base_dir=str(tmp_path))
    assignment = resolve_topology_assignment("live-exec-a", base_dir=str(tmp_path))

    assert group.main == "live-main"
    assert group.workers == ("live-exec-a", "live-exec-b")
    assert group.live_topology_mode == "multi_account"
    assert assignment is not None
    assert assignment.environment == "live"
    assert assignment.role == "executor"
    assert assignment.live_topology_mode == "multi_account"


def test_load_config_with_base_ignores_instance_overrides_for_global_configs(tmp_path) -> None:
    _write(
        tmp_path / "config" / "signal.ini",
        """
[signal]
auto_trade_enabled = true
""".strip(),
    )
    _write(
        tmp_path / "config" / "instances" / "live-main" / "signal.ini",
        """
[signal]
auto_trade_enabled = false
""".strip(),
    )

    path, parser = load_config_with_base(
        "signal.ini",
        base_dir=str(tmp_path),
        instance_name="live-main",
    )

    assert path is not None
    assert parser is not None
    assert parser["signal"]["auto_trade_enabled"] == "true"
    assert (
        get_merged_option_source(
            "signal.ini",
            "signal",
            "auto_trade_enabled",
            base_dir=str(tmp_path),
            instance_name="live-main",
        )
        == "signal.ini[signal].auto_trade_enabled"
    )


def test_load_config_with_base_applies_instance_risk_overrides(tmp_path) -> None:
    _write(
        tmp_path / "config" / "risk.ini",
        """
[risk]
max_trades_per_day = 5
""".strip(),
    )
    _write(
        tmp_path / "config" / "instances" / "live-exec-a" / "risk.ini",
        """
[risk]
max_trades_per_day = 2
""".strip(),
    )

    path, parser = load_config_with_base(
        "risk.ini",
        base_dir=str(tmp_path),
        instance_name="live-exec-a",
    )

    assert path is not None
    assert parser is not None
    assert parser["risk"]["max_trades_per_day"] == "2"
    assert (
        get_merged_option_source(
            "risk.ini",
            "risk",
            "max_trades_per_day",
            base_dir=str(tmp_path),
            instance_name="live-exec-a",
        )
        == "instances/live-exec-a/risk.ini[risk].max_trades_per_day"
    )


def test_load_mt5_settings_reads_instance_bound_account_profile(tmp_path) -> None:
    _write(
        tmp_path / "config" / "mt5.ini",
        """
[mt5]
timezone = UTC
server_time_offset_hours = 3
""".strip(),
    )
    _write(
        tmp_path / "config" / "instances" / "live-exec-a" / "mt5.ini",
        """
[mt5]
account_alias = live_exec_a
label = Live Exec A
timezone = Asia/Shanghai
""".strip(),
    )
    _write(
        tmp_path / "config" / "instances" / "live-exec-a" / "mt5.local.ini",
        """
[mt5]
login = 1002
password = secret
server = Broker-Live
path = C:/MT5/live_exec_a/terminal64.exe
""".strip(),
    )

    load_mt5_settings.cache_clear()
    try:
        settings = load_mt5_settings(
            instance_name="live-exec-a",
            base_dir=str(tmp_path),
        )
    finally:
        load_mt5_settings.cache_clear()

    assert settings.instance_name == "live-exec-a"
    assert settings.account_alias == "live_exec_a"
    assert settings.account_label == "Live Exec A"
    assert settings.mt5_login == 1002
    assert settings.mt5_server == "Broker-Live"
    assert settings.mt5_path == "C:/MT5/live_exec_a/terminal64.exe"
    assert settings.timezone == "Asia/Shanghai"
    assert settings.server_time_offset_hours == 3


def test_load_mt5_settings_does_not_inherit_root_account_credentials_into_instances(
    tmp_path,
) -> None:
    _write(
        tmp_path / "config" / "mt5.local.ini",
        """
[mt5]
login = 9999
password = root-secret
server = Root-Broker
path = C:/MT5/root/terminal64.exe
timezone = UTC
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

    load_mt5_settings.cache_clear()
    try:
        settings = load_mt5_settings(
            instance_name="live-main",
            base_dir=str(tmp_path),
        )
    finally:
        load_mt5_settings.cache_clear()

    assert settings.account_alias == "live_main"
    assert settings.mt5_login is None
    assert settings.mt5_password is None
    assert settings.mt5_server is None
    assert settings.mt5_path is None
    assert settings.timezone == "UTC"


def test_resolve_instance_scoped_dir_appends_instance_name() -> None:
    set_current_instance_name("live-main")
    try:
        assert resolve_instance_scoped_dir(Path("data") / "runtime") == Path("data") / "runtime" / "live-main"
        assert resolve_instance_scoped_dir(Path("data") / "logs") == Path("data") / "logs" / "live-main"
    finally:
        set_current_instance_name(None)
