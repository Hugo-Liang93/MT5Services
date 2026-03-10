import configparser
import os
from typing import Optional, Tuple, Dict, Any


def resolve_config_path(name_or_path: str, base_dir: Optional[str] = None) -> Optional[str]:
    """
    Resolve a config file path.
    - absolute path: return if exists
    - relative/name: resolve to <project_root>/config/<name>
    """
    if not name_or_path:
        return None
    if os.path.isabs(name_or_path):
        return name_or_path if os.path.exists(name_or_path) else None
    root = base_dir or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    path = os.path.abspath(os.path.join(root, "config", name_or_path))
    return path if os.path.exists(path) else None


def load_ini_config(name_or_path: str, base_dir: Optional[str] = None) -> Tuple[Optional[str], Optional[configparser.ConfigParser]]:
    """
    Load an ini config; returns (resolved_path, ConfigParser or None).
    """
    path = resolve_config_path(name_or_path, base_dir)
    if not path:
        return None, None
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return path, parser


def load_config_with_base(config_name: str, base_config: str = "app.ini") -> Tuple[Optional[str], Optional[configparser.ConfigParser]]:
    """
    加载配置文件，并合并基础配置（app.ini）。
    实现配置继承：特定配置继承自基础配置。
    
    Args:
        config_name: 要加载的配置文件名
        base_config: 基础配置文件名，默认为 app.ini
        
    Returns:
        (resolved_path, ConfigParser with merged config)
    """
    # 加载基础配置
    base_path, base_parser = load_ini_config(base_config)
    if not base_parser:
        # 如果没有基础配置，直接加载目标配置
        return load_ini_config(config_name)
    
    # 加载目标配置
    target_path, target_parser = load_ini_config(config_name)
    if not target_parser:
        return base_path, base_parser
    
    # 创建合并的解析器
    merged_parser = configparser.ConfigParser()
    
    # 首先添加基础配置的所有节
    for section in base_parser.sections():
        if not merged_parser.has_section(section):
            merged_parser.add_section(section)
        for key, value in base_parser.items(section):
            merged_parser.set(section, key, value)
    
    # 然后添加/覆盖目标配置
    for section in target_parser.sections():
        if not merged_parser.has_section(section):
            merged_parser.add_section(section)
        for key, value in target_parser.items(section):
            merged_parser.set(section, key, value)
    
    return target_path, merged_parser


def get_merged_config(config_name: str) -> Dict[str, Any]:
    """
    获取合并后的配置字典。
    
    Args:
        config_name: 配置文件名
        
    Returns:
        合并后的配置字典
    """
    _, parser = load_config_with_base(config_name)
    if not parser:
        return {}
    
    config_dict = {}
    for section in parser.sections():
        config_dict[section] = dict(parser.items(section))
    
    return config_dict


class ConfigValidator:
    """配置验证器，确保配置一致性"""
    
    @staticmethod
    def validate_trading_config(config: Dict[str, Any]) -> bool:
        """验证交易相关配置"""
        trading = config.get('trading', {})
        
        # 检查品种配置
        symbols = trading.get('symbols', '').split(',')
        default_symbol = trading.get('default_symbol', '')
        
        if not symbols or symbols[0].strip() == '':
            raise ValueError("No trading symbols configured")
        
        if default_symbol and default_symbol not in [s.strip() for s in symbols]:
            raise ValueError(f"Default symbol '{default_symbol}' not in symbols list: {symbols}")
        
        # 检查时间框架
        timeframes = trading.get('timeframes', '').split(',')
        valid_timeframes = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1', 'MN1']
        
        for tf in [t.strip() for t in timeframes if t.strip()]:
            if tf not in valid_timeframes:
                raise ValueError(f"Invalid timeframe: {tf}. Valid: {valid_timeframes}")
        
        return True
    
    @staticmethod
    def validate_interval_config(config: Dict[str, Any]) -> bool:
        """验证间隔配置"""
        intervals = config.get('intervals', {})
        
        # 检查采集间隔
        tick_interval = float(intervals.get('tick_interval', 0.5))
        if tick_interval < 0.1:
            raise ValueError(f"Tick interval too small: {tick_interval}")
        
        ohlc_interval = float(intervals.get('ohlc_interval', 30.0))
        if ohlc_interval < 1.0:
            raise ValueError(f"OHLC interval too small: {ohlc_interval}")
        
        return True
