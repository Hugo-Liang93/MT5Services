from __future__ import annotations

import json
from typing import Any

from src.api.admin_schemas import StrategyDetail
from src.config import resolve_config_path
from src.signals.service import SignalModule


def load_json_config(filename: str) -> Any:
    path = resolve_config_path(filename)
    if not path:
        return []
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def build_strategy_detail(name: str, signal_svc: SignalModule) -> StrategyDetail:
    return StrategyDetail(**signal_svc.describe_strategy(name))


CONFIG_FILES = [
    "app.ini",
    "signal.ini",
    "risk.ini",
    "market.ini",
    "db.ini",
    "mt5.ini",
    "ingest.ini",
    "storage.ini",
    "economic.ini",
    "cache.ini",
    "indicators.json",
]
