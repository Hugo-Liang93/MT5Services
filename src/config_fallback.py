"""
配置回退模块 - 当pydantic不可用时提供基本功能
"""

import json
import os
import configparser
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

# This module exists only as a lightweight fallback path. It still follows the
# same single-source rule as the main runtime and reads indicator settings only
# from config/indicators.json.

# 基本数据类，替代pydantic BaseModel
@dataclass
class IndicatorSettings:
    """指标设置（简化版）"""
    poll_seconds: int = 5
    reload_interval: int = 60
    backfill_enabled: bool = True
    backfill_batch_size: int = 1000
    config_path: str = "config/indicators.json"

@dataclass  
class DBSettings:
    """数据库设置（简化版）"""
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_database: str = "mt5"
    pg_schema: str = "public"

@dataclass
class IndicatorTask:
    """指标任务（简化版）"""
    name: Optional[str] = None
    func_path: str = ""
    params: Dict[str, Any] = None
    min_bars: int = 0

def load_ini_config_file(config_path: str) -> Dict[str, Dict[str, Any]]:
    """加载INI配置文件"""
    if not os.path.exists(config_path):
        return {}
    
    config = configparser.ConfigParser()
    config.read(config_path)
    
    result = {}
    for section in config.sections():
        result[section] = {}
        for key, value in config[section].items():
            # 简单类型转换
            result[section][key] = _convert_value(value)
    
    return result

def _convert_value(value: str) -> Any:
    """转换字符串值为适当类型"""
    if not value:
        return ""
    
    # 整数
    try:
        return int(value)
    except ValueError:
        pass
    
    # 浮点数
    try:
        return float(value)
    except ValueError:
        pass
    
    # 布尔值
    lower_val = value.lower()
    if lower_val in ('true', 'yes', 'on', '1'):
        return True
    elif lower_val in ('false', 'no', 'off', '0'):
        return False
    
    # 列表（逗号分隔）
    if ',' in value:
        return [item.strip() for item in value.split(',')]
    
    return value

def load_indicator_settings() -> IndicatorSettings:
    # Fallback path still reads the unified indicator config file.
    """加载指标设置"""
    config_path = "config/indicators.json"
    worker_section = {}
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                config = json.load(fh)
            worker_section = config.get("pipeline", {})
        except Exception:
            worker_section = {}
    
    return IndicatorSettings(
        poll_seconds=worker_section.get("poll_interval", 5),
        reload_interval=config.get("reload_interval", 60),
        backfill_enabled=True,
        backfill_batch_size=1000,
        config_path=config_path,
    )

def load_indicator_tasks(config_path: str = "config/indicators.json") -> List[IndicatorTask]:
    # Fallback path still reads the unified indicator config file.
    """加载指标任务"""
    if not os.path.exists(config_path):
        return []
    with open(config_path, "r", encoding="utf-8") as fh:
        config = json.load(fh)
    tasks = []
    
    for indicator in config.get("indicators", []):
        task = IndicatorTask(
            name=indicator.get("name"),
            func_path=indicator.get("func_path", ""),
            params=indicator.get("params", {}) or {},
            min_bars=int((indicator.get("params", {}) or {}).get("min_bars", 0)),
        )
        tasks.append(task)
    
    return tasks

def load_db_settings() -> DBSettings:
    """加载数据库设置"""
    config = load_ini_config_file("config/db.ini")
    db_section = config.get("db", {})
    
    return DBSettings(
        pg_host=db_section.get("pg_host", "localhost"),
        pg_port=db_section.get("pg_port", 5432),
        pg_user=db_section.get("pg_user", "postgres"),
        pg_password=db_section.get("pg_password", "postgres"),
        pg_database=db_section.get("pg_database", "mt5"),
        pg_schema=db_section.get("pg_schema", "public"),
    )

# 导出的函数和类
__all__ = [
    "IndicatorSettings",
    "DBSettings", 
    "IndicatorTask",
    "load_indicator_settings",
    "load_indicator_tasks",
    "load_db_settings",
]
