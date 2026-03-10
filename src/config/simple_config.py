"""
简化配置加载 - 不依赖pydantic
用于在没有pydantic的情况下运行系统
"""

import os
import configparser
from pathlib import Path
from typing import Dict, Any, List, Optional

def load_simple_config(config_file: str) -> Dict[str, Dict[str, Any]]:
    """加载INI配置文件，返回字典格式"""
    config = configparser.ConfigParser()
    
    # 如果文件不存在，返回空配置
    if not os.path.exists(config_file):
        return {}
    
    config.read(config_file)
    
    result = {}
    for section in config.sections():
        result[section] = {}
        for key, value in config[section].items():
            # 尝试转换为适当类型
            result[section][key] = _convert_value(value)
    
    return result

def _convert_value(value: str) -> Any:
    """将字符串值转换为适当类型"""
    if not value:
        return ""
    
    # 尝试转换为整数
    try:
        return int(value)
    except ValueError:
        pass
    
    # 尝试转换为浮点数
    try:
        return float(value)
    except ValueError:
        pass
    
    # 尝试转换为布尔值
    lower_val = value.lower()
    if lower_val in ('true', 'yes', 'on', '1'):
        return True
    elif lower_val in ('false', 'no', 'off', '0'):
        return False
    
    # 尝试转换为列表（逗号分隔）
    if ',' in value:
        items = [item.strip() for item in value.split(',')]
        # 尝试转换列表中的每个项目
        converted_items = []
        for item in items:
            converted_items.append(_convert_value(item))
        return converted_items
    
    # 保持为字符串
    return value

def get_simple_market_settings() -> Dict[str, Any]:
    """获取市场设置"""
    config = load_simple_config("config/market.ini")
    market_section = config.get("market", {})
    
    # 默认值
    defaults = {
        "default_symbol": "XAUUSD",
        "tick_limit": 200,
        "ohlc_limit": 200,
        "quote_stale_seconds": 5.0,
        "tick_cache_size": 1000,
        "ohlc_cache_limit": 500,
        "ohlc_event_queue_size": 1000,
        "intrabar_max_points": 100,
        "stream_interval_seconds": 1.0,
    }
    
    # 合并配置和默认值
    result = defaults.copy()
    result.update(market_section)
    
    return result

def get_simple_indicator_settings() -> Dict[str, Any]:
    """获取指标设置"""
    config = load_simple_config("config/indicators.ini")
    
    # 默认值
    defaults = {
        "poll_seconds": 5,
        "reload_interval": 60,
        "backfill_enabled": True,
        "backfill_batch_size": 1000,
        "config_path": "config/indicators.ini",
    }
    
    # 从worker节获取配置
    worker_section = config.get("worker", {})
    
    # 合并配置和默认值
    result = defaults.copy()
    result.update(worker_section)
    
    return result

def get_simple_ingest_settings() -> Dict[str, Any]:
    """获取采集设置"""
    config = load_simple_config("config/ingest.ini")
    ingest_section = config.get("ingest", {})
    
    # 默认值
    defaults = {
        "ingest_symbols": ["XAUUSD"],
        "ingest_timeframes": ["M1", "H1"],
        "tick_interval_seconds": 0.5,
        "ohlc_interval_seconds": 30.0,
        "tick_initial_lookback_seconds": 20,
        "ohlc_backfill_limit": 500,
    }
    
    # 合并配置和默认值
    result = defaults.copy()
    result.update(ingest_section)
    
    # 确保列表类型
    if isinstance(result.get("ingest_symbols"), str):
        result["ingest_symbols"] = [result["ingest_symbols"]]
    if isinstance(result.get("ingest_timeframes"), str):
        result["ingest_timeframes"] = [result["ingest_timeframes"]]
    
    return result

def get_simple_mt5_settings() -> Dict[str, Any]:
    """获取MT5设置"""
    config = load_simple_config("config/mt5.ini")
    mt5_section = config.get("mt5", {})
    
    # 默认值
    defaults = {
        "mt5_login": None,
        "mt5_password": None,
        "mt5_server": None,
        "mt5_path": None,
        "timezone": "UTC",
    }
    
    # 合并配置和默认值
    result = defaults.copy()
    result.update(mt5_section)
    
    return result

# 单例实例
_simple_market_settings = None
_simple_indicator_settings = None
_simple_ingest_settings = None
_simple_mt5_settings = None

def get_cached_market_settings() -> Dict[str, Any]:
    """获取缓存的market设置"""
    global _simple_market_settings
    if _simple_market_settings is None:
        _simple_market_settings = get_simple_market_settings()
    return _simple_market_settings

def get_cached_indicator_settings() -> Dict[str, Any]:
    """获取缓存的indicator设置"""
    global _simple_indicator_settings
    if _simple_indicator_settings is None:
        _simple_indicator_settings = get_simple_indicator_settings()
    return _simple_indicator_settings

def get_cached_ingest_settings() -> Dict[str, Any]:
    """获取缓存的ingest设置"""
    global _simple_ingest_settings
    if _simple_ingest_settings is None:
        _simple_ingest_settings = get_simple_ingest_settings()
    return _simple_ingest_settings

def get_cached_mt5_settings() -> Dict[str, Any]:
    """获取缓存的mt5设置"""
    global _simple_mt5_settings
    if _simple_mt5_settings is None:
        _simple_mt5_settings = get_simple_mt5_settings()
    return _simple_mt5_settings