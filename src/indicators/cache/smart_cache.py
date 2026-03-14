"""
智能缓存系统：LRU + TTL 双重缓存策略
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Optional, Dict
import time
import threading
import logging

logger = logging.getLogger(__name__)


class SmartCache:
    """
    智能缓存：LRU + TTL
    
    特性：
    1. LRU淘汰：当缓存满时淘汰最久未使用的项
    2. TTL过期：每个缓存项有过期时间
    3. 线程安全：支持多线程并发访问
    4. 统计信息：记录命中率等性能指标
    """
    
    def __init__(self, maxsize: int = 1000, ttl: int = 300):
        """
        初始化智能缓存
        
        Args:
            maxsize: 最大缓存项数
            ttl: 缓存项过期时间（秒）
        """
        self.cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self.maxsize = maxsize
        self.ttl = ttl  # 过期时间（秒）
        self.lock = threading.RLock()
        
        # 统计信息
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0
        
        logger.info(f"SmartCache initialized: maxsize={maxsize}, ttl={ttl}s")
    
    def get(self, key: str) -> Optional[Any]:
        """
        获取缓存值
        
        Args:
            key: 缓存键
            
        Returns:
            缓存值，如果不存在或已过期则返回None
        """
        with self.lock:
            if key not in self.cache:
                self.misses += 1
                logger.debug(f"Cache miss: {key}")
                return None
            
            value, timestamp = self.cache[key]
            current_time = time.time()
            
            # 检查过期
            if current_time - timestamp > self.ttl:
                del self.cache[key]
                self.expirations += 1
                self.misses += 1
                logger.debug(f"Cache expired: {key}, age={current_time - timestamp:.2f}s")
                return None
            
            # 移动到最近使用（LRU）
            self.cache.move_to_end(key)
            self.hits += 1
            logger.debug(f"Cache hit: {key}")
            return value
    
    def set(self, key: str, value: Any) -> None:
        """
        设置缓存值
        
        Args:
            key: 缓存键
            value: 缓存值
        """
        with self.lock:
            # 检查是否需要淘汰（LRU）
            if len(self.cache) >= self.maxsize:
                # 移除最久未使用
                evicted_key, _ = self.cache.popitem(last=False)
                self.evictions += 1
                logger.debug(f"Cache eviction: {evicted_key}")
            
            self.cache[key] = (value, time.time())
            logger.debug(f"Cache set: {key}")
    
    def delete(self, key: str) -> bool:
        """
        删除缓存值
        
        Args:
            key: 缓存键
            
        Returns:
            是否成功删除
        """
        with self.lock:
            if key in self.cache:
                del self.cache[key]
                logger.debug(f"Cache delete: {key}")
                return True
            return False
    
    def clear(self) -> int:
        """清空缓存"""
        with self.lock:
            cache_size = len(self.cache)
            self.cache.clear()
            
            # 重置统计信息
            self.hits = 0
            self.misses = 0
            self.evictions = 0
            self.expirations = 0
            
            logger.info(f"Cache cleared: {cache_size} items removed")
            return cache_size
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取缓存统计信息
        
        Returns:
            包含统计信息的字典
        """
        with self.lock:
            total_requests = self.hits + self.misses
            hit_rate = (self.hits / total_requests * 100) if total_requests > 0 else 0
            
            # 计算平均年龄
            current_time = time.time()
            total_age = 0
            valid_items = 0
            
            for _, timestamp in self.cache.values():
                age = current_time - timestamp
                if age <= self.ttl:  # 只统计未过期的
                    total_age += age
                    valid_items += 1
            
            avg_age = total_age / valid_items if valid_items > 0 else 0
            
            return {
                "size": len(self.cache),
                "maxsize": self.maxsize,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "expirations": self.expirations,
                "hit_rate": f"{hit_rate:.2f}%",
                "ttl": self.ttl,
                "avg_age": f"{avg_age:.2f}s",
                "total_requests": total_requests
            }
    
    def get_keys(self) -> list[str]:
        """
        获取所有缓存键
        
        Returns:
            缓存键列表
        """
        with self.lock:
            return list(self.cache.keys())
    
    def cleanup_expired(self) -> int:
        """
        清理过期缓存项
        
        Returns:
            清理的过期项数量
        """
        with self.lock:
            current_time = time.time()
            expired_keys = []
            
            for key, (_, timestamp) in self.cache.items():
                if current_time - timestamp > self.ttl:
                    expired_keys.append(key)
            
            for key in expired_keys:
                del self.cache[key]
                self.expirations += 1
            
            if expired_keys:
                logger.info(f"Cleaned up {len(expired_keys)} expired cache items")
            
            return len(expired_keys)
    
    def resize(self, new_maxsize: int) -> None:
        """
        调整缓存大小
        
        Args:
            new_maxsize: 新的最大缓存项数
        """
        with self.lock:
            if new_maxsize < self.maxsize:
                # 如果需要缩小，淘汰最久未使用的项
                while len(self.cache) > new_maxsize:
                    evicted_key, _ = self.cache.popitem(last=False)
                    self.evictions += 1
                    logger.debug(f"Resize eviction: {evicted_key}")
            
            self.maxsize = new_maxsize
            logger.info(f"Cache resized: new_maxsize={new_maxsize}")


# 全局缓存实例
_global_cache: Optional[SmartCache] = None


def get_global_cache(maxsize: int = 1000, ttl: int = 300) -> SmartCache:
    """
    获取全局缓存实例（单例模式）
    
    Args:
        maxsize: 最大缓存项数
        ttl: 缓存项过期时间（秒）
        
    Returns:
        全局缓存实例
    """
    global _global_cache
    if _global_cache is None:
        _global_cache = SmartCache(maxsize=maxsize, ttl=ttl)
    return _global_cache


def clear_global_cache() -> None:
    """清空全局缓存"""
    global _global_cache
    if _global_cache is not None:
        _global_cache.clear()


def get_global_cache_stats() -> Dict[str, Any]:
    """
    获取全局缓存统计信息
    
    Returns:
        缓存统计信息
    """
    global _global_cache
    if _global_cache is None:
        return {"error": "Global cache not initialized"}
    return _global_cache.get_stats()
