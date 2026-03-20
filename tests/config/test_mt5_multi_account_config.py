from __future__ import annotations

import configparser

from src.config.mt5 import load_mt5_accounts, load_mt5_settings


def test_load_mt5_accounts_parses_default_and_named_profiles(monkeypatch):
    parser = configparser.ConfigParser()
    parser.read_dict(
        {
            "mt5": {
                "default_account": "live",
                "path": "C:/MT5/terminal64.exe",
                "timezone": "UTC",
            },
            "account.live": {
                "label": "Live",
                "login": "1001",
                "password": "secret",
                "server": "Broker-Live",
                "enabled": "true",
            },
            "account.demo": {
                "label": "Demo",
                "login": "2002",
                "password": "secret",
                "server": "Broker-Demo",
                "enabled": "true",
                "timezone": "Asia/Shanghai",
            },
        }
    )

    monkeypatch.setattr(
        "src.config.mt5.load_config_with_base",
        lambda filename: ("config/mt5.ini", parser),
    )
    load_mt5_settings.cache_clear()

    accounts = load_mt5_accounts()

    assert list(accounts.keys())[0] == "live"
    assert accounts["live"].mt5_login == 1001
    assert accounts["demo"].timezone == "Asia/Shanghai"
    assert load_mt5_settings().account_alias == "live"
    assert load_mt5_settings("demo").mt5_server == "Broker-Demo"
