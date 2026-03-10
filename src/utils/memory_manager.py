"""
智能内存管理器
- 内存使用监控和告警
- 智能缓存清理策略
- 资源使用统计
- 内存泄漏检测（基础版）
"""

import gc
import psutil
import threading
import time
from typing import Dict, Any, List, Optional, Callable
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


class CacheStats:
    """缓存统计"""
    
    def __init__(self, name: str):
        self.name = name
        self.hits = 0
        self.misses = 0
        self.size = 0  # 估计大小（字节）
        self.last_access = 0
        self.creation_time = time.time()
    
    @property
    def hit_rate(self) -> float:
        """命中率"""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0
    
    @property
    def age(self) -> float:
        """缓存年龄（秒）"""
        return time.time() - self.creation_time
    
    def record_hit(self, size: int = 0):
        """记录命中"""
        self.hits += 1
        self.last_access = time.time()
        if size > 0:
            self.size = size
    
    def record_miss(self, size: int = 0):
        """记录未命中"""
        self.misses += 1
        if size > 0:
            self.size = size
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "name": self.name,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hit_rate,
            "size_bytes": self.size,
            "size_mb": self.size / (1024 * 1024),
            "age_seconds": self.age,
            "last_access": self.last_access
        }


class IntelligentMemoryManager:
    """
    智能内存管理器
    
    特性：
    1. 内存使用监控
    2. 智能缓存清理
    3. 资源使用统计
    4. 基础内存泄漏检测
    """
    
    def __init__(self, max_memory_mb: int = 1024, check_interval: int = 30):
        """
        初始化内存管理器
        
        Args:
            max_memory_mb: 最大内存限制（MB）
            check_interval: 检查间隔（秒）
        """
        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        self.check_interval = check_interval
        
        # 缓存注册表
        self.caches: Dict[str, CacheStats] = {}
        self.cache_objects: Dict[str, Any] = {}
        
        # 内存使用历史
        self.memory_history: List[Dict] = []
        self.max_history_size = 1000
        
        # 清理策略
        self.cleanup_threshold = 0.8  # 内存使用超过80%时触发清理
        self.min_cache_age = 300  # 最小缓存年龄（秒），避免清理新缓存
        
        # 控制标志
        self._stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        
        logger.info(f"IntelligentMemoryManager initialized with max {max_memory_mb}MB")
    
    def register_cache(self, name: str, cache_obj: Any, estimate_size: bool = True):
        """
        注册缓存对象
        
        Args:
            name: 缓存名称
            cache_obj: 缓存对象
            estimate_size: 是否估计大小
        """
        with self._lock:
            self.caches[name] = CacheStats(name)
            self.cache_objects[name] = cache_obj
            
            if estimate_size:
                self._estimate_cache_size(name, cache_obj)
            
            logger.info(f"Registered cache: {name}")
    
    def _estimate_cache_size(self, name: str, cache_obj: Any):
        """估计缓存大小（简化实现）"""
        try:
            # 尝试获取缓存大小
            if hasattr(cache_obj, '__len__'):
                size = len(cache_obj)
                # 假设每个元素平均100字节
                estimated_bytes = size * 100
                self.caches[name].size = estimated_bytes
                logger.debug(f"Estimated size for cache '{name}': {estimated_bytes} bytes")
        except Exception as e:
            logger.warning(f"Failed to estimate size for cache '{name}': {e}")
    
    def record_cache_hit(self, name: str, size: int = 0):
        """记录缓存命中"""
        with self._lock:
            if name in self.caches:
                self.caches[name].record_hit(size)
    
    def record_cache_miss(self, name: str, size: int = 0):
        """记录缓存未命中"""
        with self._lock:
            if name in self.caches:
                self.caches[name].record_miss(size)
    
    def start_monitoring(self):
        """启动内存监控"""
        if self._monitor_thread and self._monitor_thread.is_alive():
            logger.warning("Memory monitor already running")
            return
        
        self._stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="memory-monitor",
            daemon=True
        )
        self._monitor_thread.start()
        logger.info("Memory monitoring started")
    
    def stop_monitoring(self):
        """停止内存监控"""
        self._stop.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("Memory monitoring stopped")
    
    def _monitor_loop(self):
        """监控循环"""
        logger.info("Memory monitor loop started")
        
        while not self._stop.is_set():
            try:
                # 检查内存使用
                self._check_memory_usage()
                
                # 记录内存历史
                self._record_memory_snapshot()
                
                # 检查是否需要清理
                if self._should_trigger_cleanup():
                    self._perform_intelligent_cleanup()
                
            except Exception as e:
                logger.error(f"Error in memory monitor loop: {e}")
            
            # 等待下一次检查
            self._stop.wait(self.check_interval)
    
    def _check_memory_usage(self):
        """检查内存使用情况"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            
            current_usage = memory_info.rss  # 常驻内存大小
            usage_percent = current_usage / self.max_memory_bytes
            
            if usage_percent > 0.9:  # 超过90%
                logger.warning(f"High memory usage: {current_usage / (1024*1024):.1f}MB ({usage_percent:.1%})")
                
                # 触发紧急清理
                self._perform_emergency_cleanup()
                
            elif usage_percent > 0.7:  # 超过70%
                logger.info(f"Moderate memory usage: {current_usage / (1024*1024):.1f}MB ({usage_percent:.1%})")
                
        except Exception as e:
            logger.error(f"Failed to check memory usage: {e}")
    
    def _record_memory_snapshot(self):
        """记录内存快照"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            
            snapshot = {
                "timestamp": time.time(),
                "rss_mb": memory_info.rss / (1024 * 1024),
                "vms_mb": memory_info.vms / (1024 * 1024),
                "cpu_percent": process.cpu_percent(interval=0.1),
                "cache_stats": self._get_cache_stats_summary()
            }
            
            self.memory_history.append(snapshot)
            
            # 限制历史记录大小
            if len(self.memory_history) > self.max_history_size:
                self.memory_history = self.memory_history[-self.max_history_size:]
                
        except Exception as e:
            logger.error(f"Failed to record memory snapshot: {e}")
    
    def _get_cache_stats_summary(self) -> Dict[str, Any]:
        """获取缓存统计摘要"""
        with self._lock:
            total_hits = sum(cache.hits for cache in self.caches.values())
            total_misses = sum(cache.misses for cache in self.caches.values())
            total_size = sum(cache.size for cache in self.caches.values())
            
            return {
                "total_caches": len(self.caches),
                "total_hits": total_hits,
                "total_misses": total_misses,
                "total_hit_rate": total_hits / (total_hits + total_misses) if (total_hits + total_misses) > 0 else 0,
                "total_size_mb": total_size / (1024 * 1024)
            }
    
    def _should_trigger_cleanup(self) -> bool:
        """判断是否需要触发清理"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            usage_percent = memory_info.rss / self.max_memory_bytes
            
            return usage_percent > self.cleanup_threshold
            
        except Exception:
            return False
    
    def _perform_intelligent_cleanup(self):
        """执行智能清理"""
        logger.info("Performing intelligent memory cleanup")
        
        with self._lock:
            # 按缓存效率排序（命中率低、年龄大的优先清理）
            cache_metrics = []
            for name, stats in self.caches.items():
                if stats.age < self.min_cache_age:
                    continue  # 跳过新缓存
                
                # 计算清理优先级（命中率越低，优先级越高）
                priority = 1.0 - stats.hit_rate
                cache_metrics.append((name, stats, priority))
            
            # 按优先级排序
            cache_metrics.sort(key=lambda x: x[2], reverse=True)
            
            # 清理优先级最高的缓存
            cleaned_count = 0
            for name, stats, priority in cache_metrics:
                if priority > 0.3:  # 只清理优先级较高的缓存
                    if self._cleanup_cache(name):
                        cleaned_count += 1
                
                if cleaned_count >= 3:  # 每次最多清理3个缓存
                    break
            
            if cleaned_count > 0:
                logger.info(f"Cleaned {cleaned_count} caches")
                
                # 强制垃圾回收
                gc.collect()
    
    def _cleanup_cache(self, name: str) -> bool:
        """清理单个缓存"""
        try:
            cache_obj = self.cache_objects.get(name)
            if not cache_obj:
                return False
            
            # 尝试清理缓存
            if hasattr(cache_obj, 'clear'):
                cache_obj.clear()
                logger.info(f"Cleared cache: {name}")
                return True
            elif hasattr(cache_obj, 'cleanup'):
                cache_obj.cleanup()
                logger.info(f"Cleaned cache: {name}")
                return True
            else:
                logger.warning(f"Cache '{name}' has no clear/cleanup method")
                return False
                
        except Exception as e:
            logger.error(f"Failed to cleanup cache '{name}': {e}")
            return False
    
    def _perform_emergency_cleanup(self):
        """执行紧急清理"""
        logger.warning("Performing emergency memory cleanup")
        
        with self._lock:
            # 清理所有可清理的缓存
            cleaned = 0
            for name in list(self.caches.keys()):
                if self._cleanup_cache(name):
                    cleaned += 1
            
            # 强制垃圾回收
            gc.collect()
            
            logger.warning(f"Emergency cleanup completed: {cleaned} caches cleared")
    
    def get_memory_report(self) -> Dict[str, Any]:
        """获取内存使用报告"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            
            report = {
                "timestamp": time.time(),
                "process_memory": {
                    "rss_mb": memory_info.rss / (1024 * 1024),
                    "vms_mb": memory_info.vms / (1024 * 1024),
                    "percent_of_max": memory_info.rss / self.max_memory_bytes
                },
                "system_memory": {
                    "total_mb": psutil.virtual_memory().total / (1024 * 1024),
                    "available_mb": psutil.virtual_memory().available / (1024 * 1024),
                    "percent": psutil.virtual_memory().percent
                },
                "cache_stats": self._get_all_cache_stats(),
                "history_summary": self._get_history_summary(),
                "gc_stats": self._get_gc_stats()
            }
            
            return report
            
        except Exception as e:
            logger.error(f"Failed to generate memory report: {e}")
            return {"error": str(e)}
    
    def _get_all_cache_stats(self) -> Dict[str, Any]:
        """获取所有缓存统计"""
        with self._lock:
            cache_stats = {}
            for name, stats in self.caches.items():
                cache_stats[name] = stats.to_dict()
            
            summary = self._get_cache_stats_summary()
            return {
                "caches": cache_stats,
                "summary": summary
            }
    
    def _get_history_summary(self) -> Dict[str, Any]:
        """获取历史记录摘要"""
        if not self.memory_history:
            return {}
        
        recent = self.memory_history[-10:]  # 最近10个记录
        
        rss_values = [s["rss_mb"] for s in recent]
        cpu_values = [s["cpu_percent"] for s in recent]
        
        return {
            "sample_count": len(self.memory_history),
            "recent_rss_avg": sum(rss_values) / len(rss_values) if rss_values else 0,
            "recent_rss_max": max(rss_values) if rss_values else 0,
            "recent_cpu_avg": sum(cpu_values) / len(cpu_values) if cpu_values else 0
        }
    
    def _get_gc_stats(self) -> Dict[str, Any]:
        """获取垃圾回收统计"""
        return {
            "enabled": gc.isenabled(),
            "threshold": gc.get_threshold(),
            "count": gc.get_count()
        }
    
    def detect_memory_leaks(self) -> List[Dict[str, Any]]:
        """
        检测内存泄漏（基础版）
        
        通过分析内存使用趋势来检测可能的泄漏
        """
        if len(self.memory_history) < 20:
            return []  # 数据不足
        
        # 分析最近的内存使用趋势
        recent_history = self.memory_history[-20:]
        rss_values = [s["rss_mb"] for s in recent_history]
        
        # 计算线性回归斜率
        n = len(rss_values)
        x = list(range(n))
        y = rss_values
        
        sum_x = sum(x)
        sum_y = sum(y)
        sum_xy = sum(x[i] * y[i] for i in range(n))
        sum_x2 = sum(x[i] * x[i] for i in range(n))
        
        slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x * sum_x)
        
        leaks = []
        if slope > 0.1:  # 如果斜率大于0.1 MB/样本，可能存在泄漏
            leaks.append({
                "type": "memory_growth",
                "slope_mb_per_sample": slope,
                "message": f"Memory appears to be growing at {slope:.3f} MB per sample"
            })
        
        return leaks
    
    def cleanup_old_data(self, max_age_hours: int = 24):
        """清理旧数据"""
        cutoff = time.time() - (max_age_hours * 3600)
        
        with self._lock:
            # 清理旧的历史记录
            self.memory_history = [
                s for s in self.memory_history 
                if s["timestamp"] > cutoff
            ]
            
            logger.info(f"Cleaned memory history, kept {len(self.memory_history)} records")


# 单例实例
_memory_manager_instance = None

def get_memory_manager(max_memory_mb: int = 1024) -> IntelligentMemoryManager:
    """获取内存管理器单例"""
    global _memory_manager_instance
    if _memory_manager_instance is None:
        _memory_manager_instance = IntelligentMemoryManager(max_memory_mb)
    return _memory_manager_instance