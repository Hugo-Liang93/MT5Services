"""
统一指标配置系统

提供配置驱动的指标管理，支持：
1. 统一配置格式
2. 动态指标注册
3. 依赖关系配置
4. 性能参数配置
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Union
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class ComputeMode(str, Enum):
    """计算模式"""
    STANDARD = "standard"      # 标准计算
    INCREMENTAL = "incremental"  # 增量计算
    PARALLEL = "parallel"      # 并行计算


class CacheStrategy(str, Enum):
    """缓存策略"""
    NONE = "none"              # 无缓存
    SIMPLE = "simple"          # 简单缓存
    LRU_TTL = "lru_ttl"        # LRU+TTL智能缓存


@dataclass
class IndicatorConfig:
    """单个指标配置"""
    name: str                  # 指标名称
    func_path: str            # 函数路径，如 "src.indicators.mean.sma"
    params: Dict[str, Any]    # 参数，如 {"period": 20, "min_bars": 20}
    dependencies: List[str] = field(default_factory=list)  # 依赖的指标
    compute_mode: ComputeMode = ComputeMode.STANDARD  # 计算模式
    enabled: bool = True      # 是否启用
    description: str = ""     # 指标描述
    tags: List[str] = field(default_factory=list)  # 标签，如 ["trend", "momentum"]


@dataclass
class PipelineConfig:
    """流水线配置"""
    enable_parallel: bool = True      # 启用并行计算
    max_workers: int = 4              # 最大工作线程数
    enable_cache: bool = True         # 启用缓存
    cache_strategy: CacheStrategy = CacheStrategy.LRU_TTL  # 缓存策略
    cache_ttl: float = 300.0          # 缓存TTL（秒）
    cache_maxsize: int = 1000         # 缓存最大大小
    enable_incremental: bool = True   # 启用增量计算
    max_retries: int = 2              # 最大重试次数
    retry_delay: float = 0.1          # 重试延迟（秒）
    enable_monitoring: bool = True    # 启用监控
    poll_interval: float = 5.0        # 轮询间隔（秒）


@dataclass
class UnifiedIndicatorConfig:
    """统一指标配置"""
    # 指标配置
    indicators: List[IndicatorConfig] = field(default_factory=list)
    
    # 流水线配置
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    
    # 符号和时间框架配置
    symbols: List[str] = field(default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY"])
    timeframes: List[str] = field(default_factory=lambda: ["M1", "M5", "M15", "H1"])
    
    # 系统配置
    auto_start: bool = True           # 是否自动启动
    config_file: str = "config/indicators.json"  # 配置文件路径
    hot_reload: bool = True           # 是否支持热重载
    reload_interval: float = 60.0     # 热重载间隔（秒）


class ConfigLoader:
    """配置加载器"""
    
    @staticmethod
    def from_ini(filepath: str) -> UnifiedIndicatorConfig:
        """
        从INI文件加载配置（兼容现有格式）
        
        Args:
            filepath: INI文件路径
            
        Returns:
            统一配置对象
        """
        import configparser
        
        config = configparser.ConfigParser()
        config.read(filepath, encoding='utf-8')
        
        unified_config = UnifiedIndicatorConfig()
        
        # 加载流水线配置
        if 'worker' in config:
            worker_section = config['worker']
            unified_config.pipeline.poll_interval = float(
                worker_section.get('poll_seconds', '5.0')
            )
        
        # 加载指标配置
        indicators = []
        for section in config.sections():
            if section == 'worker':
                continue
                
            if 'func' in config[section]:
                indicator_config = IndicatorConfig(
                    name=section,
                    func_path=config[section]['func'],
                    params=json.loads(config[section].get('params', '{}')),
                    dependencies=[]  # INI格式不支持依赖配置
                )
                indicators.append(indicator_config)
        
        unified_config.indicators = indicators
        return unified_config
    
    @staticmethod
    def from_json(filepath: str) -> UnifiedIndicatorConfig:
        """
        从JSON文件加载配置（推荐格式）
        
        Args:
            filepath: JSON文件路径
            
        Returns:
            统一配置对象
        """
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 解析指标配置
        indicators = []
        for indicator_data in data.get('indicators', []):
            indicator_config = IndicatorConfig(
                name=indicator_data['name'],
                func_path=indicator_data['func_path'],
                params=indicator_data.get('params', {}),
                dependencies=indicator_data.get('dependencies', []),
                compute_mode=ComputeMode(indicator_data.get('compute_mode', 'standard')),
                enabled=indicator_data.get('enabled', True),
                description=indicator_data.get('description', ''),
                tags=indicator_data.get('tags', [])
            )
            indicators.append(indicator_config)
        
        # 解析流水线配置
        pipeline_data = data.get('pipeline', {})
        pipeline_config = PipelineConfig(
            enable_parallel=pipeline_data.get('enable_parallel', True),
            max_workers=pipeline_data.get('max_workers', 4),
            enable_cache=pipeline_data.get('enable_cache', True),
            cache_strategy=CacheStrategy(pipeline_data.get('cache_strategy', 'lru_ttl')),
            cache_ttl=float(pipeline_data.get('cache_ttl', 300.0)),
            cache_maxsize=pipeline_data.get('cache_maxsize', 1000),
            enable_incremental=pipeline_data.get('enable_incremental', True),
            max_retries=pipeline_data.get('max_retries', 2),
            retry_delay=float(pipeline_data.get('retry_delay', 0.1)),
            enable_monitoring=pipeline_data.get('enable_monitoring', True),
            poll_interval=float(pipeline_data.get('poll_interval', 5.0))
        )
        
        # 解析其他配置
        unified_config = UnifiedIndicatorConfig(
            indicators=indicators,
            pipeline=pipeline_config,
            symbols=data.get('symbols', ["EURUSD", "GBPUSD", "USDJPY"]),
            timeframes=data.get('timeframes', ["M1", "M5", "M15", "H1"]),
            auto_start=data.get('auto_start', True),
            config_file=filepath,
            hot_reload=data.get('hot_reload', True),
            reload_interval=float(data.get('reload_interval', 60.0))
        )
        
        return unified_config
    
    @staticmethod
    def from_yaml(filepath: str) -> UnifiedIndicatorConfig:
        """
        从YAML文件加载配置
        
        Args:
            filepath: YAML文件路径
            
        Returns:
            统一配置对象
        """
        try:
            import yaml
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            # 转换为JSON格式处理
            import tempfile
            import os
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
                json.dump(data, tmp)
                tmp_path = tmp.name
            
            try:
                config = ConfigLoader.from_json(tmp_path)
            finally:
                os.unlink(tmp_path)
            
            return config
            
        except ImportError:
            logger.warning("PyYAML not installed, falling back to JSON")
            # 尝试将YAML文件重命名为JSON
            json_path = filepath.replace('.yaml', '.json').replace('.yml', '.json')
            if Path(json_path).exists():
                return ConfigLoader.from_json(json_path)
            else:
                raise ImportError("PyYAML is required for YAML config files")
    
    @staticmethod
    def load(config_file: Optional[str] = None) -> UnifiedIndicatorConfig:
        """
        自动检测并加载配置文件
        
        Args:
            config_file: 配置文件路径，如果为None则自动检测
            
        Returns:
            统一配置对象
        """
        if config_file is None:
            # 自动检测配置文件
            possible_paths = [
                "config/indicators.json",
                "config/indicators.yaml",
                "config/indicators.yml",
                "config/indicators.ini"
            ]
            
            for path in possible_paths:
                if Path(path).exists():
                    config_file = path
                    break
            
            if config_file is None:
                # 使用默认配置
                logger.warning("No config file found, using default configuration")
                return UnifiedIndicatorConfig()
        
        # 根据文件扩展名选择加载器
        config_file = str(config_file)
        
        if config_file.endswith('.json'):
            return ConfigLoader.from_json(config_file)
        elif config_file.endswith('.yaml') or config_file.endswith('.yml'):
            return ConfigLoader.from_yaml(config_file)
        elif config_file.endswith('.ini'):
            return ConfigLoader.from_ini(config_file)
        else:
            raise ValueError(f"Unsupported config file format: {config_file}")


class ConfigManager:
    """配置管理器（支持热重载）"""
    
    def __init__(self, config_file: Optional[str] = None):
        self.config_file = config_file
        self.config: Optional[UnifiedIndicatorConfig] = None
        self._last_modified: float = 0.0
        self._lock = threading.RLock()
        
        # 初始加载配置
        self.reload()
    
    def reload(self) -> bool:
        """
        重新加载配置
        
        Returns:
            是否成功重新加载
        """
        import os
        
        with self._lock:
            try:
                # 检查文件是否被修改
                if self.config_file and os.path.exists(self.config_file):
                    current_modified = os.path.getmtime(self.config_file)
                    if current_modified <= self._last_modified:
                        return False  # 文件未修改
                    
                    self._last_modified = current_modified
                
                # 加载配置
                self.config = ConfigLoader.load(self.config_file)
                logger.info(f"Configuration reloaded from {self.config_file or 'default'}")
                return True
                
            except Exception as e:
                logger.error(f"Failed to reload configuration: {e}")
                return False
    
    def get_config(self) -> UnifiedIndicatorConfig:
        """获取当前配置"""
        with self._lock:
            if self.config is None:
                self.config = ConfigLoader.load(self.config_file)
            return self.config
    
    def save(self, filepath: Optional[str] = None) -> bool:
        """
        保存配置到文件
        
        Args:
            filepath: 文件路径，如果为None则使用当前配置文件
            
        Returns:
            是否保存成功
        """
        if filepath is None:
            filepath = self.config_file
        
        if filepath is None:
            logger.error("No filepath specified for saving configuration")
            return False
        
        with self._lock:
            try:
                config = self.get_config()
                
                # 转换为字典
                data = {
                    "indicators": [],
                    "pipeline": {
                        "enable_parallel": config.pipeline.enable_parallel,
                        "max_workers": config.pipeline.max_workers,
                        "enable_cache": config.pipeline.enable_cache,
                        "cache_strategy": config.pipeline.cache_strategy.value,
                        "cache_ttl": config.pipeline.cache_ttl,
                        "cache_maxsize": config.pipeline.cache_maxsize,
                        "enable_incremental": config.pipeline.enable_incremental,
                        "max_retries": config.pipeline.max_retries,
                        "retry_delay": config.pipeline.retry_delay,
                        "enable_monitoring": config.pipeline.enable_monitoring,
                        "poll_interval": config.pipeline.poll_interval
                    },
                    "symbols": config.symbols,
                    "timeframes": config.timeframes,
                    "auto_start": config.auto_start,
                    "hot_reload": config.hot_reload,
                    "reload_interval": config.reload_interval
                }
                
                # 添加指标配置
                for indicator in config.indicators:
                    indicator_data = {
                        "name": indicator.name,
                        "func_path": indicator.func_path,
                        "params": indicator.params,
                        "dependencies": indicator.dependencies,
                        "compute_mode": indicator.compute_mode.value,
                        "enabled": indicator.enabled,
                        "description": indicator.description,
                        "tags": indicator.tags
                    }
                    data["indicators"].append(indicator_data)
                
                # 保存到文件
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                
                logger.info(f"Configuration saved to {filepath}")
                return True
                
            except Exception as e:
                logger.error(f"Failed to save configuration: {e}")
                return False
    
    def add_indicator(self, indicator: IndicatorConfig) -> None:
        """添加指标配置"""
        with self._lock:
            config = self.get_config()
            # 检查是否已存在
            for i, existing in enumerate(config.indicators):
                if existing.name == indicator.name:
                    config.indicators[i] = indicator
                    logger.info(f"Updated indicator: {indicator.name}")
                    return
            
            # 添加新指标
            config.indicators.append(indicator)
            logger.info(f"Added new indicator: {indicator.name}")
    
    def remove_indicator(self, name: str) -> bool:
        """移除指标配置"""
        with self._lock:
            config = self.get_config()
            for i, indicator in enumerate(config.indicators):
                if indicator.name == name:
                    config.indicators.pop(i)
                    logger.info(f"Removed indicator: {name}")
                    return True
            return False
    
    def get_indicator(self, name: str) -> Optional[IndicatorConfig]:
        """获取指标配置"""
        with self._lock:
            config = self.get_config()
            for indicator in config.indicators:
                if indicator.name == name:
                    return indicator
            return None
    
    def update_pipeline_config(self, **kwargs) -> None:
        """更新流水线配置"""
        with self._lock:
            config = self.get_config()
            for key, value in kwargs.items():
                if hasattr(config.pipeline, key):
                    setattr(config.pipeline, key, value)
                    logger.info(f"Updated pipeline.{key} = {value}")


# 全局配置管理器实例
_global_config_manager: Optional[ConfigManager] = None


def get_global_config_manager(config_file: Optional[str] = None) -> ConfigManager:
    """
    获取全局配置管理器实例（单例模式）
    
    Args:
        config_file: 配置文件路径
        
    Returns:
        配置管理器实例
    """
    global _global_config_manager
    if _global_config_manager is None:
        _global_config_manager = ConfigManager(config_file)
    return _global_config_manager


def get_config() -> UnifiedIndicatorConfig:
    """
    获取当前配置（便捷函数）
    
    Returns:
        统一配置对象
    """
    return get_global_config_manager().get_config()