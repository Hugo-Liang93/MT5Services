"""Paper Trading 配置加载（config/paper_trading.ini）。"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join("config", "paper_trading.ini")
_LOCAL_CONFIG_PATH = os.path.join("config", "paper_trading.local.ini")


@dataclass
class PaperTradingConfig:
    """Paper Trading 全部可调参数。"""

    # 基础参数
    enabled: bool = False
    initial_balance: float = 10000.0
    contract_size: float = 100.0
    risk_percent: float = 1.0
    max_positions: int = 3
    min_confidence: float = 0.55

    # 交易成本
    commission_per_lot: float = 0.0
    slippage_points: float = 0.0

    # SL/TP 与持仓管理
    trailing_atr_multiplier: float = 1.0
    breakeven_atr_threshold: float = 1.0
    trailing_tp_enabled: bool = True
    trailing_tp_activation_atr: float = 1.2
    trailing_tp_trail_atr: float = 0.6

    # 日终平仓
    end_of_day_close_enabled: bool = False
    end_of_day_close_hour_utc: int = 21

    # Regime SL/TP 缩放
    regime_tp_trending: float = 1.20
    regime_tp_ranging: float = 0.80
    regime_tp_breakout: float = 1.10
    regime_tp_uncertain: float = 1.00
    regime_sl_trending: float = 1.00
    regime_sl_ranging: float = 0.90
    regime_sl_breakout: float = 1.10
    regime_sl_uncertain: float = 1.00

    # 价格监控
    price_monitor_interval: float = 1.0

    # 持久化
    persist_enabled: bool = True
    persist_flush_interval: float = 30.0

    # 进程重启恢复：启用后 bridge.start() 检测到上次未 stop 的 session 会复用 session_id
    # 并从 DB 恢复 open trades。保持默认关闭以免测试环境意外 inherit 老数据。
    resume_active_session: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（用于 session config snapshot）。"""
        return {
            k: getattr(self, k)
            for k in self.__dataclass_fields__
        }


def load_paper_trading_config() -> PaperTradingConfig:
    """从 INI 文件加载 Paper Trading 配置。

    优先级：paper_trading.local.ini > paper_trading.ini > 代码默认值
    """
    parser = configparser.ConfigParser()
    files_read = parser.read([_CONFIG_PATH, _LOCAL_CONFIG_PATH], encoding="utf-8")
    if files_read:
        logger.debug("Paper trading config loaded from: %s", files_read)

    cfg = PaperTradingConfig()

    if not parser.has_section("paper_trading"):
        return cfg

    section = parser["paper_trading"]

    _BOOL_FIELDS = {
        "enabled", "end_of_day_close_enabled", "trailing_tp_enabled",
        "persist_enabled", "resume_active_session",
    }
    _INT_FIELDS = {"max_positions", "end_of_day_close_hour_utc"}
    _FLOAT_FIELDS = {
        "initial_balance", "contract_size", "risk_percent", "min_confidence",
        "commission_per_lot", "slippage_points",
        "trailing_atr_multiplier", "breakeven_atr_threshold",
        "trailing_tp_activation_atr", "trailing_tp_trail_atr",
        "regime_tp_trending", "regime_tp_ranging", "regime_tp_breakout",
        "regime_tp_uncertain", "regime_sl_trending", "regime_sl_ranging",
        "regime_sl_breakout", "regime_sl_uncertain",
        "price_monitor_interval", "persist_flush_interval",
    }

    for key in _BOOL_FIELDS:
        if key in section:
            setattr(cfg, key, section.getboolean(key))
    for key in _INT_FIELDS:
        if key in section:
            setattr(cfg, key, section.getint(key))
    for key in _FLOAT_FIELDS:
        if key in section:
            setattr(cfg, key, section.getfloat(key))

    return cfg
