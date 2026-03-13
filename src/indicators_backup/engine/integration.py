"""
指标流水线集成器
- 将现有指标函数集成到流水线引擎
- 提供向后兼容的接口
- 支持配置驱动的指标注册
"""

import importlib
from typing import Dict, Any, Callable, List
import logging

from src.indicators.engine.pipeline_engine import IndicatorPipelineEngine, get_pipeline_engine
from src.config import load_indicator_tasks
from src.indicators.types import IndicatorTask

logger = logging.getLogger(__name__)


class IndicatorIntegration:
    """
    指标集成器
    
    负责：
    1. 加载现有指标函数
    2. 注册到流水线引擎
    3. 提供兼容接口
    4. 管理指标依赖关系
    """
    
    # 指标依赖关系定义
    INDICATOR_DEPENDENCIES = {
        # 均值类指标通常独立
        "sma": [],
        "ema": [],
        "wma": [],
        
        # 波动率指标可能需要ATR
        "bollinger": [],
        "keltner": ["atr"],
        "donchian": [],
        
        # 动量指标
        "rsi": [],
        "macd": [],  # MACD内部有依赖，但对外是独立的
        "roc": [],
        "cci": [],
        
        # 波动率指标
        "atr": [],
        
        # 成交量指标
        "obv": [],
        "vwap": [],
    }
    
    def __init__(self, config_path: str = "config/indicators.ini"):
        self.config_path = config_path
        self.pipeline_engine = get_pipeline_engine()
        self.func_registry: Dict[str, Callable] = {}
        
        # 加载内置指标函数
        self._load_builtin_indicators()
        
        logger.info("IndicatorIntegration initialized")
    
    def _load_builtin_indicators(self):
        """加载内置指标函数"""
        # 均值类指标
        try:
            from src.indicators.mean import sma, ema, wma
            self.func_registry["sma"] = sma
            self.func_registry["ema"] = ema
            self.func_registry["wma"] = wma
            logger.debug("Loaded mean indicators")
        except ImportError as e:
            logger.warning(f"Failed to load mean indicators: {e}")
        
        # 动量类指标
        try:
            from src.indicators.momentum import rsi, macd, roc, cci
            self.func_registry["rsi"] = rsi
            self.func_registry["macd"] = macd
            self.func_registry["roc"] = roc
            self.func_registry["cci"] = cci
            logger.debug("Loaded momentum indicators")
        except ImportError as e:
            logger.warning(f"Failed to load momentum indicators: {e}")
        
        # 波动率类指标
        try:
            from src.indicators.volatility import bollinger, atr, keltner, donchian
            self.func_registry["bollinger"] = bollinger
            self.func_registry["atr"] = atr
            self.func_registry["keltner"] = keltner
            self.func_registry["donchian"] = donchian
            logger.debug("Loaded volatility indicators")
        except ImportError as e:
            logger.warning(f"Failed to load volatility indicators: {e}")
        
        # 成交量类指标
        try:
            from src.indicators.volume import obv, vwap
            self.func_registry["obv"] = obv
            self.func_registry["vwap"] = vwap
            logger.debug("Loaded volume indicators")
        except ImportError as e:
            logger.warning(f"Failed to load volume indicators: {e}")
        
        logger.info(f"Loaded {len(self.func_registry)} built-in indicator functions")
    
    def _get_func_from_path(self, func_path: str) -> Callable:
        """从路径动态导入函数"""
        if func_path in self.func_registry:
            return self.func_registry[func_path]
        
        try:
            # 解析模块路径和函数名
            if "." in func_path:
                module_path, func_name = func_path.rsplit(".", 1)
            else:
                module_path = func_path
                func_name = func_path
            
            # 动态导入
            module = importlib.import_module(module_path)
            func = getattr(module, func_name)
            
            # 缓存
            self.func_registry[func_path] = func
            
            return func
            
        except Exception as e:
            logger.error(f"Failed to import function from path '{func_path}': {e}")
            raise
    
    def _extract_indicator_name(self, task: IndicatorTask) -> str:
        """从任务中提取指标名称"""
        if task.name:
            return task.name
        
        # 从函数路径中提取
        func_path = task.func_path
        if "." in func_path:
            # 例如: "src.indicators.mean.sma" -> "sma"
            return func_path.split(".")[-1]
        else:
            return func_path
    
    def _determine_dependencies(self, indicator_name: str, params: Dict[str, Any]) -> List[str]:
        """确定指标依赖关系"""
        # 基本依赖
        deps = self.INDICATOR_DEPENDENCIES.get(indicator_name.lower(), [])
        
        # 参数依赖（例如，某些指标可能需要其他指标的结果作为参数）
        # 这里可以根据实际需求扩展
        
        return deps
    
    def load_from_config(self):
        """从配置文件加载指标"""
        try:
            tasks = load_indicator_tasks(self.config_path)
            self.register_tasks(tasks)
            logger.info(f"Loaded {len(tasks)} indicators from config")
            return True
        except Exception as e:
            logger.error(f"Failed to load indicators from config: {e}")
            return False
    
    def register_tasks(self, tasks: List[IndicatorTask]):
        """注册指标任务到流水线引擎"""
        registered_count = 0
        
        for task in tasks:
            try:
                # 获取指标函数
                func = self._get_func_from_path(task.func_path)
                
                # 提取指标名称
                indicator_name = self._extract_indicator_name(task)
                
                # 确定依赖关系
                dependencies = self._determine_dependencies(indicator_name, task.params or {})
                
                # 注册到流水线引擎
                self.pipeline_engine.add_indicator(
                    name=indicator_name,
                    func=func,
                    params=task.params or {},
                    dependencies=dependencies
                )
                
                registered_count += 1
                logger.debug(f"Registered indicator: {indicator_name}")
                
            except Exception as e:
                logger.error(f"Failed to register indicator task: {task} - {e}")
        
        # 验证计算图
        errors = self.pipeline_engine.validate_graph()
        if errors:
            for error in errors:
                logger.error(f"Graph validation error: {error}")
        
        logger.info(f"Registered {registered_count} indicators to pipeline engine")
    
    def compute_indicators(self, symbol: str, timeframe: str, bars: List, 
                          use_cache: bool = True) -> Dict[str, Dict[str, float]]:
        """
        计算所有指标（兼容接口）
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            bars: K线数据
            use_cache: 是否使用缓存
            
        Returns:
            指标结果
        """
        return self.pipeline_engine.compute(symbol, timeframe, bars, use_cache)
    
    def compute_single_indicator(self, name: str, bars: List, 
                                params: Dict[str, Any] = None) -> Dict[str, float]:
        """
        计算单个指标
        
        Args:
            name: 指标名称
            bars: K线数据
            params: 指标参数
            
        Returns:
            指标结果
        """
        # 检查是否已注册
        if name not in self.pipeline_engine.nodes:
            # 尝试动态注册
            if name in self.func_registry:
                self.pipeline_engine.add_indicator(name, self.func_registry[name], params or {})
            else:
                logger.error(f"Indicator '{name}' not found and cannot be registered")
                return {}
        
        return self.pipeline_engine.compute_single(name, bars)
    
    def get_available_indicators(self) -> List[Dict[str, Any]]:
        """获取可用指标列表"""
        indicators = []
        
        # 内置指标
        for name, func in self.func_registry.items():
            indicators.append({
                "name": name,
                "type": "builtin",
                "description": func.__doc__ or "No description"
            })
        
        # 已注册到流水线的指标
        for name, node in self.pipeline_engine.nodes.items():
            if name not in [ind["name"] for ind in indicators]:
                indicators.append({
                    "name": name,
                    "type": "registered",
                    "dependencies": node.dependencies,
                    "params": node.params
                })
        
        return indicators
    
    def create_composite_indicator(self, name: str, formula: str, 
                                  dependencies: List[str] = None):
        """
        创建复合指标
        
        Args:
            name: 复合指标名称
            formula: 指标公式（字符串表达式）
            dependencies: 依赖的指标列表
            
        Example:
            create_composite_indicator("trend_strength", "rsi * 0.3 + macd_signal * 0.7", ["rsi", "macd"])
        """
        # 这里可以实现复合指标逻辑
        # 由于时间关系，先提供框架
        logger.info(f"Composite indicator '{name}' would be created with formula: {formula}")
        
        def composite_func(bars: List, params: Dict[str, Any]) -> Dict[str, float]:
            """复合指标函数（待实现）"""
            # 这里需要解析公式并计算
            # 暂时返回空结果
            return {name: 0.0}
        
        # 注册复合指标
        self.pipeline_engine.add_indicator(
            name=name,
            func=composite_func,
            params={"formula": formula},
            dependencies=dependencies or []
        )
    
    def get_performance_report(self) -> Dict[str, Any]:
        """获取性能报告"""
        pipeline_stats = self.pipeline_engine.get_performance_stats()
        
        report = {
            "integration": {
                "registered_functions": len(self.func_registry),
                "pipeline_indicators": len(self.pipeline_engine.nodes),
                "config_path": self.config_path
            },
            "pipeline": pipeline_stats,
            "available_indicators": self.get_available_indicators()
        }
        
        return report
    
    def clear_all_cache(self):
        """清空所有缓存"""
        self.pipeline_engine.clear_cache()
        logger.info("Cleared all indicator caches")
    
    def export_pipeline_config(self) -> Dict[str, Any]:
        """导出流水线配置"""
        return self.pipeline_engine.export_config()
    
    def visualize_pipeline(self, format: str = "mermaid") -> str:
        """可视化流水线"""
        return self.pipeline_engine.visualize_graph(format)


# 向后兼容的包装器
class CompatibleIndicatorWorker:
    """
    兼容的指标工作器
    
    提供与原有IndicatorWorker兼容的接口，
    但内部使用流水线引擎
    """
    
    def __init__(self, integration: IndicatorIntegration):
        self.integration = integration
        self.pipeline_engine = integration.pipeline_engine
    
    def compute(self, symbol: str, timeframe: str, bars: List) -> Dict[str, Any]:
        """计算指标（兼容原有接口）"""
        results = self.integration.compute_indicators(symbol, timeframe, bars)
        
        # 转换为原有格式
        flattened = {}
        for indicator_name, indicator_results in results.items():
            if isinstance(indicator_results, dict):
                for key, value in indicator_results.items():
                    flattened[key] = value
            else:
                flattened[indicator_name] = indicator_results
        
        return flattened
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return self.integration.get_performance_report()


# 单例实例
_integration_instance = None

def get_indicator_integration(config_path: str = "config/indicators.ini") -> IndicatorIntegration:
    """获取指标集成器单例"""
    global _integration_instance
    if _integration_instance is None:
        _integration_instance = IndicatorIntegration(config_path)
    return _integration_instance