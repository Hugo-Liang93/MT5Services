"""
指标计算流水线引擎
- 支持指标依赖关系
- 并行计算优化
- 复合指标支持
- 计算图可视化
"""

import threading
import time
from typing import Dict, List, Any, Optional, Callable, Set, Tuple
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

logger = logging.getLogger(__name__)


class IndicatorNode:
    """指标计算节点"""
    
    def __init__(self, name: str, func: Callable, params: Dict[str, Any], 
                 dependencies: List[str] = None):
        self.name = name
        self.func = func
        self.params = params or {}
        self.dependencies = dependencies or []
        self.result: Optional[Dict[str, float]] = None
        self.execution_time: float = 0
        self.last_executed: float = 0
        self.success_count: int = 0
        self.failure_count: int = 0
    
    def execute(self, bars: List, dependency_results: Dict[str, Dict[str, float]]) -> Dict[str, float]:
        """执行指标计算"""
        start_time = time.time()
        
        try:
            # 准备参数（包含依赖结果）
            exec_params = self.params.copy()
            
            # 添加依赖结果到参数中
            for dep_name in self.dependencies:
                if dep_name in dependency_results:
                    exec_params.update(dependency_results[dep_name])
            
            # 执行指标函数
            result = self.func(bars, exec_params)
            
            if not isinstance(result, dict):
                # 如果返回单个值，转换为字典
                result = {self.name: result}
            
            self.result = result
            self.execution_time = time.time() - start_time
            self.last_executed = time.time()
            self.success_count += 1
            
            logger.debug(f"Indicator '{self.name}' executed in {self.execution_time:.3f}s")
            return result
            
        except Exception as e:
            self.failure_count += 1
            logger.error(f"Failed to execute indicator '{self.name}': {e}")
            return {}
    
    @property
    def success_rate(self) -> float:
        """计算成功率"""
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "name": self.name,
            "dependencies": self.dependencies,
            "success_rate": self.success_rate,
            "execution_time_avg": self.execution_time if self.success_count == 1 else 0,
            "last_executed": self.last_executed,
            "total_executions": self.success_count + self.failure_count
        }


class IndicatorPipelineEngine:
    """
    指标计算流水线引擎
    
    特性：
    1. 支持指标依赖关系
    2. 并行计算优化
    3. 智能缓存策略
    4. 计算图可视化
    5. 性能监控
    """
    
    def __init__(self, max_workers: int = 4, cache_size: int = 1000):
        self.nodes: Dict[str, IndicatorNode] = {}
        self.dependency_graph: Dict[str, List[str]] = defaultdict(list)  # 正向依赖
        self.reverse_dependency: Dict[str, List[str]] = defaultdict(list)  # 反向依赖
        self.execution_order: List[List[str]] = []  # 分层执行顺序
        
        # 并行计算
        self.max_workers = max_workers
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        
        # 缓存
        self.cache_size = cache_size
        self.result_cache: Dict[str, Dict] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        
        # 性能统计
        self.total_executions = 0
        self.total_execution_time = 0
        
        # 线程安全
        self._lock = threading.Lock()
        
        logger.info(f"IndicatorPipelineEngine initialized with {max_workers} workers")
    
    def add_indicator(self, name: str, func: Callable, params: Dict[str, Any] = None, 
                      dependencies: List[str] = None):
        """
        添加指标到计算图
        
        Args:
            name: 指标名称
            func: 指标计算函数
            params: 指标参数
            dependencies: 依赖的指标名称列表
        """
        with self._lock:
            if name in self.nodes:
                logger.warning(f"Indicator '{name}' already exists, updating")
            
            # 创建节点
            node = IndicatorNode(name, func, params, dependencies)
            self.nodes[name] = node
            
            # 更新依赖图
            if dependencies:
                self.dependency_graph[name] = dependencies.copy()
                for dep in dependencies:
                    self.reverse_dependency[dep].append(name)
            else:
                self.dependency_graph[name] = []
            
            # 重新构建执行顺序
            self._build_execution_order()
            
            logger.info(f"Added indicator '{name}' with {len(dependencies or [])} dependencies")
    
    def _build_execution_order(self):
        """构建分层执行顺序（拓扑排序）"""
        # 计算入度
        in_degree = {node: 0 for node in self.nodes}
        for node, deps in self.dependency_graph.items():
            in_degree[node] = len(deps)
        
        # 拓扑排序（分层）
        self.execution_order = []
        remaining = set(self.nodes.keys())
        
        while remaining:
            # 找到当前入度为0的节点
            current_level = []
            for node in list(remaining):
                if in_degree[node] == 0:
                    current_level.append(node)
            
            if not current_level:
                # 存在环，无法排序
                logger.error("Circular dependency detected in indicator graph")
                break
            
            # 添加到当前层
            self.execution_order.append(current_level)
            
            # 移除当前层节点，更新入度
            for node in current_level:
                remaining.remove(node)
                for dependent in self.reverse_dependency[node]:
                    in_degree[dependent] -= 1
        
        logger.debug(f"Built execution order with {len(self.execution_order)} levels")
    
    def compute(self, symbol: str, timeframe: str, bars: List, 
                use_cache: bool = True) -> Dict[str, Dict[str, float]]:
        """
        计算所有指标
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            bars: K线数据
            use_cache: 是否使用缓存
            
        Returns:
            指标结果字典 {指标名称: {指标键: 值}}
        """
        start_time = time.time()
        
        # 生成缓存键
        cache_key = self._generate_cache_key(symbol, timeframe, bars)
        
        if use_cache and cache_key in self.result_cache:
            self.cache_hits += 1
            logger.debug(f"Cache hit for {symbol}/{timeframe}")
            return self.result_cache[cache_key]
        
        self.cache_misses += 1
        
        # 按层并行计算
        all_results = {}
        dependency_results = {}
        
        for level in self.execution_order:
            level_results = self._compute_level(level, bars, dependency_results)
            
            # 合并结果
            all_results.update(level_results)
            dependency_results.update(level_results)
        
        # 更新缓存
        if use_cache:
            self._update_cache(cache_key, all_results)
        
        # 更新统计
        execution_time = time.time() - start_time
        self.total_executions += 1
        self.total_execution_time += execution_time
        
        logger.info(f"Computed {len(all_results)} indicators for {symbol}/{timeframe} in {execution_time:.3f}s")
        
        return all_results
    
    def _compute_level(self, level_nodes: List[str], bars: List, 
                      dependency_results: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
        """并行计算一层指标"""
        if not level_nodes:
            return {}
        
        # 准备任务
        futures = {}
        for node_name in level_nodes:
            node = self.nodes[node_name]
            
            # 检查依赖是否满足
            missing_deps = [dep for dep in node.dependencies if dep not in dependency_results]
            if missing_deps:
                logger.warning(f"Indicator '{node_name}' missing dependencies: {missing_deps}")
                continue
            
            # 提交任务
            future = self.executor.submit(node.execute, bars, dependency_results)
            futures[node_name] = future
        
        # 收集结果
        level_results = {}
        for node_name, future in futures.items():
            try:
                result = future.result(timeout=10)  # 10秒超时
                if result:
                    level_results[node_name] = result
            except Exception as e:
                logger.error(f"Failed to compute indicator '{node_name}': {e}")
        
        return level_results
    
    def _generate_cache_key(self, symbol: str, timeframe: str, bars: List) -> str:
        """生成缓存键"""
        if not bars:
            return f"{symbol}_{timeframe}_empty"
        
        # 使用最后一条K线的时间作为缓存键的一部分
        last_bar = bars[-1]
        bar_time = last_bar.time.isoformat() if hasattr(last_bar, 'time') else str(len(bars))
        
        # 简化缓存键
        return f"{symbol}_{timeframe}_{bar_time}"
    
    def _update_cache(self, cache_key: str, results: Dict[str, Dict[str, float]]):
        """更新缓存"""
        with self._lock:
            # 限制缓存大小
            if len(self.result_cache) >= self.cache_size:
                # 移除最旧的缓存项
                oldest_key = next(iter(self.result_cache))
                del self.result_cache[oldest_key]
            
            self.result_cache[cache_key] = results
    
    def compute_single(self, name: str, bars: List, 
                      dependency_results: Dict[str, Dict[str, float]] = None) -> Dict[str, float]:
        """
        计算单个指标
        
        Args:
            name: 指标名称
            bars: K线数据
            dependency_results: 依赖指标结果
            
        Returns:
            指标结果
        """
        if name not in self.nodes:
            logger.error(f"Indicator '{name}' not found")
            return {}
        
        node = self.nodes[name]
        return node.execute(bars, dependency_results or {})
    
    def get_dependency_graph(self) -> Dict[str, Any]:
        """获取依赖图"""
        return {
            "nodes": list(self.nodes.keys()),
            "edges": [(node, dep) for node, deps in self.dependency_graph.items() for dep in deps],
            "execution_order": self.execution_order
        }
    
    def visualize_graph(self, format: str = "mermaid") -> str:
        """
        可视化计算图
        
        Args:
            format: 输出格式 ("mermaid" 或 "dot")
            
        Returns:
            图描述字符串
        """
        if format == "mermaid":
            return self._generate_mermaid_graph()
        elif format == "dot":
            return self._generate_dot_graph()
        else:
            logger.error(f"Unsupported graph format: {format}")
            return ""
    
    def _generate_mermaid_graph(self) -> str:
        """生成Mermaid图"""
        lines = ["graph TD"]
        
        # 添加节点
        for node_name in self.nodes:
            lines.append(f"    {node_name}[{node_name}]")
        
        # 添加边
        for node_name, deps in self.dependency_graph.items():
            for dep in deps:
                lines.append(f"    {dep} --> {node_name}")
        
        # 按执行顺序分组
        for i, level in enumerate(self.execution_order):
            if len(level) > 1:
                level_str = " & ".join(level)
                lines.append(f"    subgraph Level {i}")
                lines.append(f"        direction LR")
                for node in level:
                    lines.append(f"        {node}")
                lines.append(f"    end")
        
        return "\n".join(lines)
    
    def _generate_dot_graph(self) -> str:
        """生成Graphviz DOT图"""
        lines = [
            "digraph IndicatorGraph {",
            "    rankdir=TB;",
            "    node [shape=box, style=filled, fillcolor=lightblue];"
        ]
        
        # 添加节点
        for node_name in self.nodes:
            node = self.nodes[node_name]
            success_rate = node.success_rate
            fillcolor = "lightgreen" if success_rate > 0.9 else "lightyellow" if success_rate > 0.7 else "lightcoral"
            
            lines.append(f'    "{node_name}" [fillcolor="{fillcolor}", label="{node_name}\\nSR: {success_rate:.1%}"];')
        
        # 添加边
        for node_name, deps in self.dependency_graph.items():
            for dep in deps:
                lines.append(f'    "{dep}" -> "{node_name}";')
        
        # 按执行顺序设置层级
        for i, level in enumerate(self.execution_order):
            if level:
                level_str = "; ".join(f'"{node}"' for node in level)
                lines.append(f"    {{rank=same; {level_str}}}")
        
        lines.append("}")
        return "\n".join(lines)
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """获取性能统计"""
        with self._lock:
            node_stats = {}
            for name, node in self.nodes.items():
                node_stats[name] = node.to_dict()
            
            avg_execution_time = self.total_execution_time / self.total_executions if self.total_executions > 0 else 0
            
            return {
                "total_indicators": len(self.nodes),
                "total_executions": self.total_executions,
                "avg_execution_time": avg_execution_time,
                "cache_stats": {
                    "hits": self.cache_hits,
                    "misses": self.cache_misses,
                    "hit_rate": self.cache_hits / (self.cache_hits + self.cache_misses) if (self.cache_hits + self.cache_misses) > 0 else 0,
                    "size": len(self.result_cache)
                },
                "node_stats": node_stats,
                "dependency_levels": len(self.execution_order)
            }
    
    def clear_cache(self):
        """清空缓存"""
        with self._lock:
            self.result_cache.clear()
            self.cache_hits = 0
            self.cache_misses = 0
            logger.info("Indicator cache cleared")
    
    def validate_graph(self) -> List[str]:
        """验证计算图"""
        errors = []
        
        # 检查未定义的依赖
        for node_name, deps in self.dependency_graph.items():
            for dep in deps:
                if dep not in self.nodes:
                    errors.append(f"Node '{node_name}' depends on undefined node '{dep}'")
        
        # 检查循环依赖
        visited = set()
        recursion_stack = set()
        
        def dfs(node: str) -> bool:
            visited.add(node)
            recursion_stack.add(node)
            
            for neighbor in self.dependency_graph[node]:
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in recursion_stack:
                    return True
            
            recursion_stack.remove(node)
            return False
        
        for node in self.nodes:
            if node not in visited:
                if dfs(node):
                    errors.append("Circular dependency detected")
                    break
        
        return errors
    
    def export_config(self) -> Dict[str, Any]:
        """导出配置"""
        config = {
            "indicators": {},
            "max_workers": self.max_workers,
            "cache_size": self.cache_size
        }
        
        for name, node in self.nodes.items():
            config["indicators"][name] = {
                "params": node.params,
                "dependencies": node.dependencies
            }
        
        return config
    
    def import_config(self, config: Dict[str, Any], func_registry: Dict[str, Callable]):
        """
        导入配置
        
        Args:
            config: 配置字典
            func_registry: 函数注册表 {函数名: 函数}
        """
        indicator_configs = config.get("indicators", {})
        
        for name, ind_config in indicator_configs.items():
            func_name = ind_config.get("func") or name
            
            if func_name not in func_registry:
                logger.warning(f"Function '{func_name}' not found in registry for indicator '{name}'")
                continue
            
            self.add_indicator(
                name=name,
                func=func_registry[func_name],
                params=ind_config.get("params", {}),
                dependencies=ind_config.get("dependencies", [])
            )
        
        logger.info(f"Imported {len(indicator_configs)} indicators from config")


# 单例实例
_pipeline_engine_instance = None

def get_pipeline_engine(max_workers: int = 4) -> IndicatorPipelineEngine:
    """获取流水线引擎单例"""
    global _pipeline_engine_instance
    if _pipeline_engine_instance is None:
        _pipeline_engine_instance = IndicatorPipelineEngine(max_workers)
    return _pipeline_engine_instance