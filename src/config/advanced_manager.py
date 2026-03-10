"""
高级配置管理器
- 配置热加载和变更监听
- 配置验证和类型安全
- 环境变量覆盖支持
- 配置版本管理
"""

import os
import time
import json
import threading
import configparser
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, asdict
import logging

logger = logging.getLogger(__name__)


@dataclass
class ConfigChangeEvent:
    """配置变更事件"""
    config_file: str
    section: str
    key: str
    old_value: Any
    new_value: Any
    timestamp: float


class ConfigWatcher:
    """配置变更监听器（简化版，不依赖watchdog）"""
    
    def __init__(self, config_dir: str, check_interval: int = 5):
        self.config_dir = Path(config_dir)
        self.check_interval = check_interval
        self.file_mtimes: Dict[str, float] = {}
        self.callbacks: List[Callable] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        
        # 初始化文件修改时间
        self._init_file_mtimes()
    
    def _init_file_mtimes(self):
        """初始化文件修改时间记录"""
        for config_file in self.config_dir.glob("*.ini"):
            try:
                self.file_mtimes[str(config_file)] = config_file.stat().st_mtime
            except Exception:
                pass
    
    def register_callback(self, callback: Callable[[ConfigChangeEvent], None]):
        """注册配置变更回调"""
        self.callbacks.append(callback)
    
    def start(self):
        """启动监听"""
        if self._thread and self._thread.is_alive():
            return
        
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="config-watcher",
            daemon=True
        )
        self._thread.start()
        logger.info("Config watcher started")
    
    def stop(self):
        """停止监听"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Config watcher stopped")
    
    def _watch_loop(self):
        """监听循环（轮询方式）"""
        logger.info("Config watch loop started")
        
        while not self._stop.is_set():
            try:
                self._check_for_changes()
            except Exception as e:
                logger.error(f"Error in config watch loop: {e}")
            
            # 等待下一次检查
            self._stop.wait(self.check_interval)
    
    def _check_for_changes(self):
        """检查配置变更"""
        for config_file in self.config_dir.glob("*.ini"):
            file_path = str(config_file)
            
            try:
                current_mtime = config_file.stat().st_mtime
                last_mtime = self.file_mtimes.get(file_path)
                
                if last_mtime is None:
                    # 新文件
                    self.file_mtimes[file_path] = current_mtime
                    logger.info(f"New config file detected: {config_file.name}")
                    continue
                
                if current_mtime > last_mtime:
                    # 文件已修改
                    logger.info(f"Config file modified: {config_file.name}")
                    self.file_mtimes[file_path] = current_mtime
                    
                    # 解析变更
                    changes = self._parse_config_changes(config_file, last_mtime)
                    for change in changes:
                        self._notify_callbacks(change)
                        
            except Exception as e:
                logger.error(f"Failed to check config file {config_file}: {e}")
    
    def _parse_config_changes(self, config_file: Path, last_mtime: float) -> List[ConfigChangeEvent]:
        """解析配置变更（简化实现）"""
        # 这里可以更精细地比较配置差异
        # 当前实现只通知文件已修改
        return [ConfigChangeEvent(
            config_file=str(config_file),
            section="*",
            key="*",
            old_value=None,
            new_value=None,
            timestamp=time.time()
        )]
    
    def _notify_callbacks(self, event: ConfigChangeEvent):
        """通知所有回调"""
        for callback in self.callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"Error in config change callback: {e}")


class AdvancedConfigManager:
    """
    高级配置管理器
    
    特性：
    1. 类型安全的配置获取
    2. 配置变更热加载
    3. 环境变量覆盖
    4. 配置验证
    5. 默认值管理
    """
    
    # 配置schema定义
    CONFIG_SCHEMA = {
        "app.ini": {
            "trading.symbols": {
                "type": "list",
                "default": ["XAUUSD"],
                "env_var": "MT5_SYMBOLS",
                "description": "交易品种列表"
            },
            "trading.timeframes": {
                "type": "list", 
                "default": ["M1", "H1"],
                "env_var": "MT5_TIMEFRAMES",
                "description": "时间框架列表"
            },
            "trading.default_symbol": {
                "type": "str",
                "default": "XAUUSD",
                "env_var": "MT5_DEFAULT_SYMBOL",
                "description": "默认交易品种"
            },
            "intervals.tick_interval": {
                "type": "float",
                "default": 0.5,
                "env_var": "TICK_INTERVAL",
                "min": 0.1,
                "max": 10.0,
                "description": "tick数据采集间隔（秒）"
            },
            "intervals.ohlc_interval": {
                "type": "float",
                "default": 30.0,
                "env_var": "OHLC_INTERVAL",
                "min": 1.0,
                "max": 300.0,
                "description": "K线数据采集间隔（秒）"
            },
            "system.log_level": {
                "type": "str",
                "default": "INFO",
                "env_var": "LOG_LEVEL",
                "allowed": ["DEBUG", "INFO", "WARNING", "ERROR"],
                "description": "日志级别"
            }
        },
        "indicators.ini": {
            "worker.poll_seconds": {
                "type": "int",
                "default": 5,
                "env_var": "INDICATOR_POLL_SECONDS",
                "min": 1,
                "max": 60,
                "description": "指标计算轮询间隔（秒）"
            },
            "worker.reload_interval": {
                "type": "int",
                "default": 60,
                "env_var": "INDICATOR_RELOAD_INTERVAL",
                "min": 10,
                "max": 3600,
                "description": "配置重载间隔（秒）"
            }
        }
    }
    
    def __init__(self, config_dir: str = "config"):
        self.config_dir = Path(config_dir)
        self.configs: Dict[str, configparser.ConfigParser] = {}
        self.watcher = ConfigWatcher(config_dir)
        self._lock = threading.Lock()
        
        # 加载所有配置
        self._load_all_configs()
        
        # 启动配置监听
        self.watcher.register_callback(self._on_config_change)
        self.watcher.start()
        
        logger.info(f"AdvancedConfigManager initialized with config dir: {config_dir}")
    
    def _load_all_configs(self):
        """加载所有配置文件"""
        for config_file in self.config_dir.glob("*.ini"):
            self._load_config(config_file.name)
    
    def _load_config(self, filename: str):
        """加载单个配置文件"""
        config_path = self.config_dir / filename
        
        if not config_path.exists():
            logger.warning(f"Config file not found: {filename}")
            return
        
        try:
            config = configparser.ConfigParser()
            config.read(config_path)
            
            with self._lock:
                self.configs[filename] = config
            
            logger.info(f"Loaded config: {filename}")
            
        except Exception as e:
            logger.error(f"Failed to load config {filename}: {e}")
    
    def get(self, filename: str, section: str, key: str, default: Any = None) -> Any:
        """
        类型安全的配置获取
        
        Args:
            filename: 配置文件名（如 "app.ini"）
            section: 配置节
            key: 配置键
            default: 默认值
            
        Returns:
            配置值（已进行类型转换）
        """
        # 检查schema定义
        schema_key = f"{section}.{key}"
        schema = self.CONFIG_SCHEMA.get(filename, {}).get(schema_key)
        
        # 首先检查环境变量
        env_value = None
        if schema and "env_var" in schema:
            env_value = os.environ.get(schema["env_var"])
        
        if env_value is not None:
            # 使用环境变量值
            value = env_value
            source = "env"
        else:
            # 从配置文件获取
            with self._lock:
                config = self.configs.get(filename)
                if config and config.has_option(section, key):
                    value = config.get(section, key)
                    source = "file"
                else:
                    value = None
                    source = None
        
        # 如果都没有，使用默认值
        if value is None:
            if schema and "default" in schema:
                value = schema["default"]
                source = "default"
            else:
                value = default
                source = "param"
        
        # 类型转换和验证
        if schema:
            value = self._convert_type(value, schema.get("type", "str"))
            value = self._validate_value(value, schema)
        
        logger.debug(f"Config get: {filename}[{section}].{key} = {value} (source: {source})")
        return value
    
    def _convert_type(self, value: str, type_name: str) -> Any:
        """类型转换"""
        if value is None:
            return None
        
        try:
            if type_name == "int":
                return int(value)
            elif type_name == "float":
                return float(value)
            elif type_name == "bool":
                return value.lower() in ["true", "yes", "1", "on"]
            elif type_name == "list":
                # 支持逗号分隔的列表
                if isinstance(value, str):
                    return [item.strip() for item in value.split(",") if item.strip()]
                elif isinstance(value, list):
                    return value
                else:
                    return [str(value)]
            else:  # str or other
                return str(value)
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to convert value '{value}' to type '{type_name}': {e}")
            return value
    
    def _validate_value(self, value: Any, schema: Dict) -> Any:
        """验证配置值"""
        # 检查允许的值
        if "allowed" in schema and value not in schema["allowed"]:
            logger.warning(f"Value '{value}' not in allowed values: {schema['allowed']}")
            return schema.get("default", value)
        
        # 检查数值范围
        if isinstance(value, (int, float)):
            if "min" in schema and value < schema["min"]:
                logger.warning(f"Value '{value}' below minimum {schema['min']}")
                return schema.get("min", value)
            if "max" in schema and value > schema["max"]:
                logger.warning(f"Value '{value}' above maximum {schema['max']}")
                return schema.get("max", value)
        
        return value
    
    def get_all(self, filename: str, section: str) -> Dict[str, Any]:
        """获取整个配置节"""
        with self._lock:
            config = self.configs.get(filename)
            if not config or not config.has_section(section):
                return {}
            
            result = {}
            for key in config.options(section):
                result[key] = self.get(filename, section, key)
            
            return result
    
    def set(self, filename: str, section: str, key: str, value: Any):
        """
        设置配置值（临时，不持久化）
        
        注意：这只会修改内存中的配置，不会写入文件
        """
        with self._lock:
            if filename not in self.configs:
                self.configs[filename] = configparser.ConfigParser()
            
            config = self.configs[filename]
            if not config.has_section(section):
                config.add_section(section)
            
            config.set(section, key, str(value))
            logger.info(f"Config set: {filename}[{section}].{key} = {value}")
    
    def _on_config_change(self, event: ConfigChangeEvent):
        """配置变更回调"""
        logger.info(f"Config file changed: {event.config_file}")
        
        # 重新加载配置
        filename = Path(event.config_file).name
        self._load_config(filename)
        
        # 通知其他组件（可以通过事件总线实现）
        self._notify_config_change(filename)
    
    def _notify_config_change(self, filename: str):
        """通知配置变更（简化实现）"""
        # 这里可以集成到事件系统
        logger.info(f"Notifying components about config change: {filename}")
    
    def validate_all(self) -> Dict[str, List[str]]:
        """验证所有配置"""
        errors = {}
        
        for filename, schema in self.CONFIG_SCHEMA.items():
            file_errors = []
            
            for schema_key, rule in schema.items():
                section, key = schema_key.split(".", 1)
                
                try:
                    value = self.get(filename, section, key)
                    
                    # 验证类型
                    expected_type = rule.get("type", "str")
                    if not self._check_type(value, expected_type):
                        file_errors.append(f"{schema_key}: expected {expected_type}, got {type(value).__name__}")
                    
                    # 验证范围
                    if isinstance(value, (int, float)):
                        if "min" in rule and value < rule["min"]:
                            file_errors.append(f"{schema_key}: value {value} below minimum {rule['min']}")
                        if "max" in rule and value > rule["max"]:
                            file_errors.append(f"{schema_key}: value {value} above maximum {rule['max']}")
                    
                    # 验证允许值
                    if "allowed" in rule and value not in rule["allowed"]:
                        file_errors.append(f"{schema_key}: value {value} not in allowed values {rule['allowed']}")
                        
                except Exception as e:
                    file_errors.append(f"{schema_key}: {e}")
            
            if file_errors:
                errors[filename] = file_errors
        
        return errors
    
    def _check_type(self, value: Any, expected_type: str) -> bool:
        """检查类型"""
        type_map = {
            "int": (int,),
            "float": (float, int),
            "bool": (bool,),
            "str": (str,),
            "list": (list,)
        }
        
        expected_types = type_map.get(expected_type, (object,))
        return isinstance(value, expected_types)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取配置管理器统计"""
        return {
            "config_files": list(self.configs.keys()),
            "watcher_running": self.watcher._thread.is_alive() if self.watcher._thread else False,
            "validation_errors": self.validate_all()
        }
    
    def stop(self):
        """停止配置管理器"""
        self.watcher.stop()
        logger.info("AdvancedConfigManager stopped")


# 单例实例
_config_manager_instance = None

def get_config_manager(config_dir: str = "config") -> AdvancedConfigManager:
    """获取配置管理器单例"""
    global _config_manager_instance
    if _config_manager_instance is None:
        _config_manager_instance = AdvancedConfigManager(config_dir)
    return _config_manager_instance