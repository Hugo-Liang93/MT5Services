"""
集中式配置管理系统 - 实现单一信号源配置
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from src.config.utils import load_config_with_base, get_merged_config, ConfigValidator


class TradingConfig(BaseModel):
    """交易核心配置 - 单一信号源"""
    symbols: List[str] = Field(default_factory=lambda: ["XAUUSD"])
    timeframes: List[str] = Field(default_factory=lambda: ["M1", "H1"])
    default_symbol: str = "XAUUSD"


class IntervalConfig(BaseModel):
    """间隔配置"""
    tick_interval: float = 0.5
    ohlc_interval: float = 30.0
    stream_interval: float = 1.0
    indicator_reload_interval: float = 60.0


class LimitConfig(BaseModel):
    """限制配置"""
    tick_limit: int = 200
    ohlc_limit: int = 200
    tick_cache_size: int = 5000
    ohlc_cache_limit: int = 500
    quote_stale_seconds: float = 1.0


class SystemConfig(BaseModel):
    """系统配置"""
    timezone: str = "UTC"
    log_level: str = "INFO"
    modules_enabled: List[str] = Field(default_factory=lambda: ["ingest", "api", "indicators", "storage"])


class APIConfig(BaseModel):
    """API特有配置"""
    host: str = "0.0.0.0"
    port: int = 8808
    enable_cors: bool = True
    docs_enabled: bool = True
    redoc_enabled: bool = True
    auth_enabled: bool = False
    api_key_header: str = "X-API-Key"
    access_log_enabled: bool = True
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class IngestConfig(BaseModel):
    """数据采集特有配置"""
    tick_initial_lookback_seconds: int = 20
    ohlc_backfill_limit: int = 500
    retry_attempts: int = 3
    retry_backoff: float = 1.0
    connection_timeout: float = 10.0
    max_concurrent_symbols: int = 5
    queue_monitor_interval: float = 5.0
    health_check_interval: float = 30.0
    max_allowed_delay: float = 60.0


class CentralizedConfig:
    """集中式配置管理器"""
    
    def __init__(self):
        self._config_cache: Dict[str, Any] = {}
        self._validated = False
    
    def load_all(self) -> Dict[str, Any]:
        """加载所有配置"""
        if not self._config_cache:
            self._load_and_validate()
        return self._config_cache
    
    def _load_and_validate(self):
        """加载并验证所有配置"""
        # 加载主配置
        main_config = get_merged_config("app.ini")
        
        # 加载并合并其他配置
        configs = {
            "main": main_config,
            "api": get_merged_config("market.ini"),
            "ingest": get_merged_config("ingest.ini"),
            "mt5": get_merged_config("mt5.ini"),
            "db": get_merged_config("db.ini"),
            "storage": get_merged_config("storage.ini"),
            "cache": get_merged_config("cache.ini"),
            "indicators": get_merged_config("indicators.ini"),
        }
        
        # 提取共享配置
        shared_config = self._extract_shared_config(main_config)
        
        # 验证配置一致性
        self._validate_configs(shared_config, configs)
        
        # 构建最终配置
        self._build_final_config(shared_config, configs)
        
        self._validated = True
    
    def _extract_shared_config(self, main_config: Dict[str, Any]) -> Dict[str, Any]:
        """从主配置提取共享配置"""
        return {
            "trading": TradingConfig(**main_config.get("trading", {})).dict(),
            "intervals": IntervalConfig(**main_config.get("intervals", {})).dict(),
            "limits": LimitConfig(**main_config.get("limits", {})).dict(),
            "system": SystemConfig(**main_config.get("system", {})).dict(),
        }
    
    def _validate_configs(self, shared_config: Dict[str, Any], configs: Dict[str, Dict[str, Any]]):
        """验证所有配置的一致性"""
        # 验证交易配置
        ConfigValidator.validate_trading_config(shared_config)
        ConfigValidator.validate_interval_config(shared_config)
        
        # 检查配置继承关系
        self._check_config_inheritance(shared_config, configs)
    
    def _check_config_inheritance(self, shared_config: Dict[str, Any], configs: Dict[str, Dict[str, Any]]):
        """检查配置继承关系"""
        trading_symbols = shared_config["trading"]["symbols"]
        
        # 检查ingest配置是否使用正确的品种
        ingest_config = configs["ingest"]
        # ingest应该从主配置继承品种，不应该有自己的品种配置
        
        # 检查API默认品种是否在交易品种中
        api_config = configs["api"]
        if "default_symbol" in api_config.get("api", {}):
            api_default = api_config["api"]["default_symbol"]
            if api_default not in trading_symbols:
                raise ValueError(
                    f"API default symbol '{api_default}' not in trading symbols: {trading_symbols}"
                )
    
    def _build_final_config(self, shared_config: Dict[str, Any], configs: Dict[str, Dict[str, Any]]):
        """构建最终配置结构"""
        self._config_cache = {
            # 共享配置
            **shared_config,
            
            # 模块特有配置
            "api": APIConfig(**configs["api"].get("api", {})).dict(),
            "ingest": IngestConfig(**configs["ingest"].get("ingest", {})).dict(),
            
            # 原始配置（保持兼容）
            "raw": {
                "mt5": configs["mt5"],
                "db": configs["db"],
                "storage": configs["storage"],
                "cache": configs["cache"],
                "indicators": configs["indicators"],
            }
        }
    
    def get_trading_config(self) -> TradingConfig:
        """获取交易配置"""
        config = self.load_all()
        return TradingConfig(**config["trading"])
    
    def get_interval_config(self) -> IntervalConfig:
        """获取间隔配置"""
        config = self.load_all()
        return IntervalConfig(**config["intervals"])
    
    def get_limit_config(self) -> LimitConfig:
        """获取限制配置"""
        config = self.load_all()
        return LimitConfig(**config["limits"])
    
    def get_api_config(self) -> APIConfig:
        """获取API配置"""
        config = self.load_all()
        return APIConfig(**config["api"])
    
    def get_ingest_config(self) -> IngestConfig:
        """获取采集配置"""
        config = self.load_all()
        return IngestConfig(**config["ingest"])
    
    def get_system_config(self) -> SystemConfig:
        """获取系统配置"""
        config = self.load_all()
        return SystemConfig(**config["system"])
    
    def get_raw_config(self, module: str) -> Dict[str, Any]:
        """获取原始模块配置（兼容性）"""
        config = self.load_all()
        return config["raw"].get(module, {})
    
    def reload(self):
        """重新加载配置"""
        self._config_cache.clear()
        self._validated = False
        self.load_all()


# 全局配置实例
_config_manager = CentralizedConfig()


@lru_cache
def get_trading_config() -> TradingConfig:
    """获取交易配置（缓存）"""
    return _config_manager.get_trading_config()


@lru_cache
def get_interval_config() -> IntervalConfig:
    """获取间隔配置（缓存）"""
    return _config_manager.get_interval_config()


@lru_cache
def get_limit_config() -> LimitConfig:
    """获取限制配置（缓存）"""
    return _config_manager.get_limit_config()


@lru_cache
def get_api_config() -> APIConfig:
    """获取API配置（缓存）"""
    return _config_manager.get_api_config()


@lru_cache
def get_ingest_config() -> IngestConfig:
    """获取采集配置（缓存）"""
    return _config_manager.get_ingest_config()


@lru_cache
def get_system_config() -> SystemConfig:
    """获取系统配置（缓存）"""
    return _config_manager.get_system_config()


def reload_configs():
    """重新加载所有配置"""
    get_trading_config.cache_clear()
    get_interval_config.cache_clear()
    get_limit_config.cache_clear()
    get_api_config.cache_clear()
    get_ingest_config.cache_clear()
    get_system_config.cache_clear()
    _config_manager.reload()


# 兼容性函数（逐步迁移）
def get_shared_symbols() -> List[str]:
    """获取共享的交易品种列表"""
    return get_trading_config().symbols


def get_shared_timeframes() -> List[str]:
    """获取共享的时间框架列表"""
    return get_trading_config().timeframes


def get_shared_default_symbol() -> str:
    """获取共享的默认品种"""
    return get_trading_config().default_symbol


def validate_config_consistency():
    """验证配置一致性（供外部调用）"""
    try:
        _config_manager.load_all()
        return True, "配置验证通过"
    except Exception as e:
        return False, f"配置验证失败: {e}"