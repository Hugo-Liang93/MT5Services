"""
依赖关系管理器

功能：
1. 管理指标间依赖关系
2. 提供拓扑排序的执行顺序
3. 检测循环依赖
4. 可视化依赖图
"""

from __future__ import annotations

from typing import Dict, Set, List, Optional, Tuple
from collections import defaultdict, deque
import logging

logger = logging.getLogger(__name__)


class DependencyManager:
    """
    依赖关系管理器
    
    管理指标间的依赖关系，提供执行顺序优化
    """
    
    def __init__(self):
        """
        初始化依赖关系管理器
        """
        self.graph: Dict[str, Set[str]] = defaultdict(set)  # 正向依赖：指标 -> 依赖的指标
        self.reverse_graph: Dict[str, Set[str]] = defaultdict(set)  # 反向依赖：指标 <- 依赖于此的指标
        self.indicator_params: Dict[str, Dict] = {}  # 指标参数
        self.indicator_funcs: Dict[str, callable] = {}  # 指标函数
        self.indicator_cache_ttl: Dict[str, int] = {}  # per-indicator cache TTL (秒)
        
        logger.info("DependencyManager initialized")
    
    def add_indicator(
        self,
        name: str,
        func: callable,
        params: Dict,
        dependencies: Optional[List[str]] = None
    ) -> None:
        """
        添加指标及其依赖
        
        Args:
            name: 指标名称
            func: 指标计算函数
            params: 指标参数
            dependencies: 依赖的指标列表
        """
        # 存储指标信息
        self.indicator_params[name] = params
        self.indicator_funcs[name] = func
        
        # 添加依赖关系
        if dependencies:
            for dep in dependencies:
                self.add_dependency(name, dep)
        
        logger.debug(f"Indicator added: {name}, dependencies: {dependencies or []}")
    
    def add_dependency(self, indicator: str, depends_on: str) -> None:
        """
        添加依赖关系
        
        Args:
            indicator: 依赖的指标
            depends_on: 被依赖的指标
        """
        # 检查是否添加自己
        if indicator == depends_on:
            logger.warning(f"Indicator '{indicator}' cannot depend on itself")
            return
        
        # 添加正向依赖
        self.graph[indicator].add(depends_on)
        
        # 添加反向依赖
        self.reverse_graph[depends_on].add(indicator)
        
        logger.debug(f"Dependency added: {indicator} -> {depends_on}")
    
    def remove_dependency(self, indicator: str, depends_on: str) -> None:
        """
        移除依赖关系
        
        Args:
            indicator: 依赖的指标
            depends_on: 被依赖的指标
        """
        if indicator in self.graph:
            self.graph[indicator].discard(depends_on)
            if not self.graph[indicator]:
                del self.graph[indicator]
        
        if depends_on in self.reverse_graph:
            self.reverse_graph[depends_on].discard(indicator)
            if not self.reverse_graph[depends_on]:
                del self.reverse_graph[depends_on]
        
        logger.debug(f"Dependency removed: {indicator} -> {depends_on}")
    
    def get_dependencies(self, indicator: str) -> Set[str]:
        """
        获取指标依赖
        
        Args:
            indicator: 指标名称
            
        Returns:
            依赖的指标集合
        """
        return self.graph.get(indicator, set())
    
    def get_dependents(self, indicator: str) -> Set[str]:
        """
        获取依赖此指标的指标
        
        Args:
            indicator: 指标名称
            
        Returns:
            依赖此指标的指标集合
        """
        return self.reverse_graph.get(indicator, set())
    
    def get_all_dependencies(self, indicator: str, include_self: bool = False) -> Set[str]:
        """
        获取所有依赖（递归）
        
        Args:
            indicator: 指标名称
            include_self: 是否包含自己
            
        Returns:
            所有依赖的指标集合
        """
        visited = set()
        stack = [indicator]
        
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            
            visited.add(current)
            
            # 添加当前指标的依赖
            for dep in self.graph.get(current, set()):
                if dep not in visited:
                    stack.append(dep)
        
        if not include_self:
            visited.discard(indicator)
        
        return visited
    
    def get_execution_order(self, indicators: Optional[List[str]] = None) -> List[List[str]]:
        """
        获取拓扑排序的执行顺序（按层级分组）
        
        Args:
            indicators: 需要计算的指标列表，如果为None则计算所有
            
        Returns:
            按层级分组的执行顺序
        """
        # 确定要计算的指标集合
        if indicators is None:
            target_indicators = set(self.indicator_funcs.keys())
        else:
            target_indicators = set(indicators)
        
        # 获取所有相关节点（目标指标及其所有依赖）
        relevant_nodes = set()
        for indicator in target_indicators:
            relevant_nodes.add(indicator)
            # 添加所有依赖（递归）
            stack = [indicator]
            while stack:
                current = stack.pop()
                for dep in self.graph.get(current, set()):
                    if dep not in relevant_nodes:
                        relevant_nodes.add(dep)
                        stack.append(dep)
        
        # 构建子图的入度
        in_degree = {node: 0 for node in relevant_nodes}
        
        for node in relevant_nodes:
            for neighbor in self.graph.get(node, set()):
                if neighbor in relevant_nodes:
                    in_degree[neighbor] += 1
        
        # 拓扑排序（Kahn算法）
        result = []
        queue = deque([node for node in relevant_nodes if in_degree[node] == 0])
        
        while queue:
            level_size = len(queue)
            current_level = []
            
            for _ in range(level_size):
                node = queue.popleft()
                current_level.append(node)
                
                # 减少当前节点所依赖的节点的入度
                for neighbor in self.graph.get(node, set()):
                    if neighbor in relevant_nodes:
                        in_degree[neighbor] -= 1
                        if in_degree[neighbor] == 0:
                            queue.append(neighbor)
            
            result.append(current_level)
        
        # 检查是否有环
        remaining_nodes = [node for node in relevant_nodes if in_degree[node] > 0]
        if remaining_nodes:
            raise ValueError(f"Circular dependency detected among: {remaining_nodes}")
        
        # 反转结果，得到从底层（无依赖）到顶层（依赖最多）的顺序
        result.reverse()
        
        logger.debug(f"Execution order computed: {len(result)} levels, {len(relevant_nodes)} nodes")
        return result
    
    def get_parallelizable_groups(self, indicators: Optional[List[str]] = None) -> List[List[str]]:
        """
        获取可并行计算的组
        
        Args:
            indicators: 需要计算的指标列表
            
        Returns:
            可并行计算的指标分组
        """
        execution_order = self.get_execution_order(indicators)
        return execution_order
    
    def validate_graph(self) -> List[str]:
        """
        验证依赖图
        
        Returns:
            错误信息列表
        """
        errors = []
        
        # 检查循环依赖
        try:
            self.get_execution_order()
        except ValueError as e:
            errors.append(str(e))
        
        # 检查未定义的依赖
        all_nodes = set(self.graph.keys()) | set(self.reverse_graph.keys())
        defined_nodes = set(self.indicator_funcs.keys())
        
        for node in all_nodes:
            if node not in defined_nodes:
                errors.append(f"Undefined indicator referenced: '{node}'")
            
            for dep in self.graph.get(node, set()):
                if dep not in defined_nodes:
                    errors.append(f"Indicator '{node}' depends on undefined indicator '{dep}'")
        
        # 检查孤立节点（没有依赖也没有被依赖）
        for node in defined_nodes:
            if (node not in self.graph or not self.graph[node]) and \
               (node not in self.reverse_graph or not self.reverse_graph[node]):
                logger.debug(f"Isolated indicator: {node}")
                # 孤立节点不是错误，只是记录
        
        if errors:
            logger.warning(f"Dependency graph validation found {len(errors)} errors")
        else:
            logger.info("Dependency graph validation passed")
        
        return errors
    
    def get_indicator_info(self, name: str) -> Optional[Dict[str, any]]:
        """
        获取指标信息
        
        Args:
            name: 指标名称
            
        Returns:
            指标信息字典，如果不存在则返回None
        """
        if name not in self.indicator_funcs:
            return None
        
        return {
            "name": name,
            "params": self.indicator_params.get(name, {}),
            "dependencies": list(self.get_dependencies(name)),
            "dependents": list(self.get_dependents(name)),
            "all_dependencies": list(self.get_all_dependencies(name)),
            "has_func": name in self.indicator_funcs
        }
    
    def get_all_indicators(self) -> List[Dict[str, any]]:
        """
        获取所有指标信息
        
        Returns:
            指标信息列表
        """
        result = []
        for name in self.indicator_funcs.keys():
            info = self.get_indicator_info(name)
            if info:
                result.append(info)
        
        return result
    
    def visualize(self, format: str = "mermaid") -> str:
        """
        可视化依赖图
        
        Args:
            format: 输出格式，支持 "mermaid" 或 "dot"
            
        Returns:
            可视化字符串
        """
        if format == "mermaid":
            return self._visualize_mermaid()
        elif format == "dot":
            return self._visualize_dot()
        else:
            raise ValueError(f"Unsupported format: {format}")
    
    def _visualize_mermaid(self) -> str:
        """生成Mermaid格式的可视化"""
        lines = ["graph TD"]
        
        # 添加节点和边
        for node in sorted(self.graph.keys()):
            for dep in sorted(self.graph[node]):
                lines.append(f"    {dep} --> {node}")
        
        # 添加孤立节点
        defined_nodes = set(self.indicator_funcs.keys())
        for node in defined_nodes:
            if node not in self.graph or not self.graph[node]:
                # 检查是否有反向依赖
                if node not in self.reverse_graph or not self.reverse_graph[node]:
                    lines.append(f"    {node}")
        
        return "\n".join(lines)
    
    def _visualize_dot(self) -> str:
        """生成Graphviz DOT格式的可视化"""
        lines = [
            "digraph DependencyGraph {",
            "    rankdir=LR;",
            "    node [shape=box, style=filled, fillcolor=lightblue];"
        ]
        
        # 添加节点和边
        for node in sorted(self.graph.keys()):
            for dep in sorted(self.graph[node]):
                lines.append(f'    "{dep}" -> "{node}";')
        
        # 添加孤立节点
        defined_nodes = set(self.indicator_funcs.keys())
        for node in defined_nodes:
            if node not in self.graph or not self.graph[node]:
                if node not in self.reverse_graph or not self.reverse_graph[node]:
                    lines.append(f'    "{node}";')
        
        lines.append("}")
        return "\n".join(lines)
    
    def clear(self) -> None:
        """清空所有依赖关系"""
        self.graph.clear()
        self.reverse_graph.clear()
        self.indicator_params.clear()
        self.indicator_funcs.clear()
        
        logger.info("DependencyManager cleared")
    
    def remove_indicator(self, name: str) -> bool:
        """
        移除指标
        
        Args:
            name: 指标名称
            
        Returns:
            是否成功移除
        """
        if name not in self.indicator_funcs:
            return False
        
        # 移除正向依赖
        if name in self.graph:
            del self.graph[name]
        
        # 移除反向依赖
        if name in self.reverse_graph:
            for dependent in self.reverse_graph[name]:
                self.graph[dependent].discard(name)
                if not self.graph[dependent]:
                    del self.graph[dependent]
            del self.reverse_graph[name]
        
        # 从其他指标的反向依赖中移除
        for node in list(self.reverse_graph.keys()):
            if name in self.reverse_graph[node]:
                self.reverse_graph[node].discard(name)
                if not self.reverse_graph[node]:
                    del self.reverse_graph[node]
        
        # 移除指标信息
        if name in self.indicator_params:
            del self.indicator_params[name]
        
        if name in self.indicator_funcs:
            del self.indicator_funcs[name]
        
        logger.info(f"Indicator removed: {name}")
        return True
    
    def export_config(self) -> Dict[str, any]:
        """
        导出配置
        
        Returns:
            配置字典
        """
        config = {
            "indicators": {},
            "dependencies": []
        }
        
        # 导出指标信息
        for name, params in self.indicator_params.items():
            config["indicators"][name] = {
                "params": params,
                "has_func": name in self.indicator_funcs
            }
        
        # 导出依赖关系
        for indicator, deps in self.graph.items():
            for dep in deps:
                config["dependencies"].append({
                    "from": dep,
                    "to": indicator
                })
        
        return config
    
    def import_config(self, config: Dict[str, any]) -> None:
        """
        导入配置
        
        Args:
            config: 配置字典
        """
        self.clear()
        
        # 导入指标（需要外部提供函数）
        for name, info in config.get("indicators", {}).items():
            self.indicator_params[name] = info.get("params", {})
            # 函数需要外部注册
        
        # 导入依赖关系
        for dep_info in config.get("dependencies", []):
            self.add_dependency(dep_info["to"], dep_info["from"])
        
        logger.info(f"Config imported: {len(self.indicator_params)} indicators")


# 全局依赖管理器实例
_global_dependency_manager: Optional[DependencyManager] = None


def get_global_dependency_manager() -> DependencyManager:
    """
    获取全局依赖管理器实例（单例模式）
    
    Returns:
        全局依赖管理器实例
    """
    global _global_dependency_manager
    if _global_dependency_manager is None:
        _global_dependency_manager = DependencyManager()
    return _global_dependency_manager


def clear_global_dependency_manager() -> None:
    """清空全局依赖管理器"""
    global _global_dependency_manager
    if _global_dependency_manager is not None:
        _global_dependency_manager.clear()
        _global_dependency_manager = None