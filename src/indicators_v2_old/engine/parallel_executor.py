"""
并行计算框架

功能：
1. 支持无依赖指标的并行计算
2. 线程池管理
3. 任务调度和结果收集
4. 错误处理和重试机制
"""

from __future__ import annotations

import concurrent.futures
from typing import Dict, List, Any, Optional, Callable, Tuple
import threading
import time
import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskResult:
    """任务结果"""
    task_id: str
    status: TaskStatus
    result: Any
    error: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    retry_count: int = 0
    
    @property
    def duration(self) -> Optional[float]:
        """任务耗时（秒）"""
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return None
    
    @property
    def success(self) -> bool:
        """是否成功"""
        return self.status == TaskStatus.COMPLETED


class ParallelExecutor:
    """
    并行计算执行器
    
    特性：
    1. 线程池管理
    2. 任务调度和依赖处理
    3. 结果缓存和复用
    4. 错误处理和重试
    5. 性能监控
    """
    
    def __init__(
        self,
        max_workers: int = 4,
        max_retries: int = 2,
        retry_delay: float = 0.1,
        enable_cache: bool = True,
        cache_ttl: float = 300.0
    ):
        """
        初始化并行执行器
        
        Args:
            max_workers: 最大工作线程数
            max_retries: 最大重试次数
            retry_delay: 重试延迟（秒）
            enable_cache: 是否启用结果缓存
            cache_ttl: 缓存过期时间（秒）
        """
        self.max_workers = max_workers
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.enable_cache = enable_cache
        self.cache_ttl = cache_ttl
        
        # 线程池
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        
        # 任务状态
        self.tasks: Dict[str, TaskResult] = {}
        self.task_lock = threading.RLock()
        
        # 结果缓存
        self.result_cache: Dict[str, Tuple[Any, float]] = {}
        self.cache_lock = threading.RLock()
        
        # 性能统计
        self.stats = {
            "total_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
            "retried_tasks": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "total_duration": 0.0
        }
        self.stats_lock = threading.RLock()
        
        logger.info(
            f"ParallelExecutor initialized: "
            f"max_workers={max_workers}, "
            f"max_retries={max_retries}, "
            f"enable_cache={enable_cache}"
        )
    
    def _generate_task_id(self, func_name: str, args_hash: int) -> str:
        """
        生成任务ID
        
        Args:
            func_name: 函数名称
            args_hash: 参数哈希
            
        Returns:
            任务ID
        """
        return f"{func_name}_{args_hash}"
    
    def _get_cached_result(self, task_id: str) -> Optional[Any]:
        """
        获取缓存结果
        
        Args:
            task_id: 任务ID
            
        Returns:
            缓存结果，如果不存在或已过期则返回None
        """
        if not self.enable_cache:
            return None
        
        with self.cache_lock:
            if task_id not in self.result_cache:
                with self.stats_lock:
                    self.stats["cache_misses"] += 1
                return None
            
            result, timestamp = self.result_cache[task_id]
            
            # 检查过期
            if time.time() - timestamp > self.cache_ttl:
                del self.result_cache[task_id]
                with self.stats_lock:
                    self.stats["cache_misses"] += 1
                return None
            
            with self.stats_lock:
                self.stats["cache_hits"] += 1
            
            logger.debug(f"Cache hit: {task_id}")
            return result
    
    def _set_cached_result(self, task_id: str, result: Any) -> None:
        """
        设置缓存结果
        
        Args:
            task_id: 任务ID
            result: 计算结果
        """
        if not self.enable_cache:
            return
        
        with self.cache_lock:
            self.result_cache[task_id] = (result, time.time())
            logger.debug(f"Cache set: {task_id}")
    
    def _execute_task(
        self,
        task_id: str,
        func: Callable,
        args: Tuple,
        kwargs: Dict[str, Any]
    ) -> TaskResult:
        """
        执行单个任务
        
        Args:
            task_id: 任务ID
            func: 执行函数
            args: 位置参数
            kwargs: 关键字参数
            
        Returns:
            任务结果
        """
        start_time = time.time()
        result = None
        error = None
        status = TaskStatus.COMPLETED
        
        try:
            # 执行函数
            result = func(*args, **kwargs)
            
        except Exception as e:
            error = str(e)
            status = TaskStatus.FAILED
            logger.error(f"Task {task_id} failed: {error}")
        
        end_time = time.time()
        
        return TaskResult(
            task_id=task_id,
            status=status,
            result=result,
            error=error,
            start_time=start_time,
            end_time=end_time
        )
    
    def submit_task(
        self,
        func: Callable,
        args: Tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        use_cache: bool = True
    ) -> str:
        """
        提交任务
        
        Args:
            func: 执行函数
            args: 位置参数
            kwargs: 关键字参数
            task_id: 任务ID（如果为None则自动生成）
            use_cache: 是否使用缓存
            
        Returns:
            任务ID
        """
        if kwargs is None:
            kwargs = {}
        
        # 生成任务ID
        if task_id is None:
            args_hash = hash(str(args) + str(kwargs))
            task_id = self._generate_task_id(func.__name__, args_hash)
        
        # 检查缓存
        if use_cache and self.enable_cache:
            cached_result = self._get_cached_result(task_id)
            if cached_result is not None:
                # 立即返回缓存结果
                with self.task_lock:
                    self.tasks[task_id] = TaskResult(
                        task_id=task_id,
                        status=TaskStatus.COMPLETED,
                        result=cached_result,
                        start_time=time.time(),
                        end_time=time.time()
                    )
                
                with self.stats_lock:
                    self.stats["total_tasks"] += 1
                    self.stats["completed_tasks"] += 1
                
                logger.debug(f"Task {task_id} completed from cache")
                return task_id
        
        # 提交到线程池
        future = self.executor.submit(
            self._execute_task_with_retry,
            task_id, func, args, kwargs, use_cache
        )
        
        # 存储任务信息
        with self.task_lock:
            self.tasks[task_id] = TaskResult(
                task_id=task_id,
                status=TaskStatus.PENDING,
                result=None,
                start_time=time.time()
            )
        
        with self.stats_lock:
            self.stats["total_tasks"] += 1
        
        # 设置回调
        future.add_done_callback(lambda f: self._task_done_callback(task_id, f))
        
        logger.debug(f"Task {task_id} submitted")
        return task_id
    
    def _execute_task_with_retry(
        self,
        task_id: str,
        func: Callable,
        args: Tuple,
        kwargs: Dict[str, Any],
        use_cache: bool
    ) -> TaskResult:
        """
        执行任务（带重试）
        
        Args:
            task_id: 任务ID
            func: 执行函数
            args: 位置参数
            kwargs: 关键字参数
            use_cache: 是否使用缓存
            
        Returns:
            任务结果
        """
        retry_count = 0
        
        while retry_count <= self.max_retries:
            # 更新任务状态
            with self.task_lock:
                if task_id in self.tasks:
                    self.tasks[task_id].status = TaskStatus.RUNNING
                    self.tasks[task_id].retry_count = retry_count
            
            # 执行任务
            result = self._execute_task(task_id, func, args, kwargs)
            
            # 如果成功或达到最大重试次数，返回结果
            if result.success or retry_count >= self.max_retries:
                return result
            
            # 重试
            retry_count += 1
            
            with self.stats_lock:
                self.stats["retried_tasks"] += 1
            
            logger.warning(f"Task {task_id} failed, retrying ({retry_count}/{self.max_retries})")
            
            # 等待重试延迟
            if self.retry_delay > 0:
                time.sleep(self.retry_delay)
        
        return result
    
    def _task_done_callback(self, task_id: str, future: concurrent.futures.Future) -> None:
        """
        任务完成回调
        
        Args:
            task_id: 任务ID
            future: Future对象
        """
        try:
            result = future.result()
            
            with self.task_lock:
                if task_id in self.tasks:
                    self.tasks[task_id] = result
            
            # 更新统计
            with self.stats_lock:
                if result.success:
                    self.stats["completed_tasks"] += 1
                    if result.duration:
                        self.stats["total_duration"] += result.duration
                    
                    # 缓存结果
                    if self.enable_cache:
                        self._set_cached_result(task_id, result.result)
                else:
                    self.stats["failed_tasks"] += 1
            
            logger.debug(f"Task {task_id} completed with status: {result.status.value}")
            
        except Exception as e:
            logger.error(f"Error in task done callback for {task_id}: {e}")
    
    def get_task_result(self, task_id: str, timeout: Optional[float] = None) -> Optional[TaskResult]:
        """
        获取任务结果
        
        Args:
            task_id: 任务ID
            timeout: 超时时间（秒）
            
        Returns:
            任务结果，如果超时或任务不存在则返回None
        """
        start_time = time.time()
        
        while True:
            with self.task_lock:
                if task_id in self.tasks:
                    result = self.tasks[task_id]
                    if result.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
                        return result
            
            # 检查超时
            if timeout is not None and time.time() - start_time > timeout:
                logger.warning(f"Timeout waiting for task {task_id}")
                return None
            
            # 短暂等待
            time.sleep(0.01)
    
    def wait_for_tasks(
        self,
        task_ids: List[str],
        timeout: Optional[float] = None
    ) -> Dict[str, Optional[TaskResult]]:
        """
        等待多个任务完成
        
        Args:
            task_ids: 任务ID列表
            timeout: 超时时间（秒）
            
        Returns:
            任务结果字典
        """
        results = {}
        start_time = time.time()
        
        for task_id in task_ids:
            results[task_id] = None
        
        remaining_tasks = set(task_ids)
        
        while remaining_tasks:
            completed_tasks = set()
            
            for task_id in remaining_tasks:
                result = self.get_task_result(task_id, timeout=0)
                if result is not None:
                    results[task_id] = result
                    completed_tasks.add(task_id)
            
            remaining_tasks -= completed_tasks
            
            # 检查超时
            if timeout is not None and time.time() - start_time > timeout:
                logger.warning(f"Timeout waiting for {len(remaining_tasks)} tasks")
                break
            
            # 如果还有任务在运行，等待一下
            if remaining_tasks:
                time.sleep(0.05)
        
        return results
    
    def execute_parallel(
        self,
        tasks: List[Tuple[Callable, Tuple, Dict[str, Any]]],
        task_ids: Optional[List[str]] = None,
        use_cache: bool = True
    ) -> Dict[str, Optional[TaskResult]]:
        """
        并行执行多个任务
        
        Args:
            tasks: 任务列表，每个元素为 (func, args, kwargs)
            task_ids: 任务ID列表（如果为None则自动生成）
            use_cache: 是否使用缓存
            
        Returns:
            任务结果字典
        """
        if task_ids is None:
            task_ids = []
            for func, args, kwargs in tasks:
                args_hash = hash(str(args) + str(kwargs))
                task_id = self._generate_task_id(func.__name__, args_hash)
                task_ids.append(task_id)
        
        # 提交所有任务
        submitted_ids = []
        for (func, args, kwargs), task_id in zip(tasks, task_ids):
            submitted_id = self.submit_task(func, args, kwargs, task_id, use_cache)
            submitted_ids.append(submitted_id)
        
        # 等待所有任务完成
        results = self.wait_for_tasks(submitted_ids)
        
        return results
    
    def cancel_task(self, task_id: str) -> bool:
        """
        取消任务
        
        Args:
            task_id: 任务ID
            
        Returns:
            是否成功取消
        """
        # 注意：Python的ThreadPoolExecutor不支持取消已提交的任务
        # 这里只是标记任务状态为取消
        with self.task_lock:
            if task_id in self.tasks:
                self.tasks[task_id].status = TaskStatus.CANCELLED
                logger.info(f"Task {task_id} cancelled")
                return True
        
        return False
    
    def clear_cache(self) -> int:
        """
        清空缓存
        
        Returns:
            清除的缓存项数量
        """
        with self.cache_lock:
            count = len(self.result_cache)
            self.result_cache.clear()
            
            logger.info(f"Cache cleared: {count} items removed")
            return count
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计信息字典
        """
        with self.stats_lock:
            stats = self.stats.copy()
            
            # 计算平均耗时
            if stats["completed_tasks"] > 0:
                stats["avg_duration"] = stats["total_duration"] / stats["completed_tasks"]
            else:
                stats["avg_duration"] = 0
            
            # 计算成功率
            if stats["total_tasks"] > 0:
                stats["success_rate"] = stats["completed_tasks"] / stats["total_tasks"] * 100
            else:
                stats["success_rate"] = 0
            
            # 计算缓存命中率
            total_cache_access = stats["cache_hits"] + stats["cache_misses"]
            if total_cache_access > 0:
                stats["cache_hit_rate"] = stats["cache_hits"] / total_cache_access * 100
            else:
                stats["cache_hit_rate"] = 0
            
            stats["cache_size"] = len(self.result_cache)
            stats["pending_tasks"] = len([t for t in self.tasks.values() if t.status == TaskStatus.PENDING])
            stats["running_tasks"] = len([t for t in self.tasks.values() if t.status == TaskStatus.RUNNING])
            
            return stats
    
    def shutdown(self, wait: bool = True) -> None:
        """
        关闭执行器
        
        Args:
            wait: 是否等待任务完成
        """
        self.executor.shutdown(wait=wait)
        logger.info("ParallelExecutor shutdown")
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.shutdown(wait=True)


# 全局并行执行器实例
_global_executor: Optional[ParallelExecutor] = None


def get_global_executor(
    max_workers: int = 4,
    max_retries: int = 2,
    retry_delay: float = 0.1,
    enable_cache: bool = True,
    cache_ttl: float = 300.0
) -> ParallelExecutor:
    """
    获取全局并行执行器实例（单例模式）
    
    Args:
        max_workers: 最大工作线程数
        max_retries: 最大重试次数
        retry_delay: 重试延迟（秒）
        enable_cache: 是否启用结果缓存
        cache_ttl: 缓存过期时间（秒）
        
    Returns:
        全局并行执行器实例
    """
    global _global_executor
    if _global_executor is None:
        _global_executor = ParallelExecutor(
            max_workers=max_workers,
            max_retries=max_retries,
            retry_delay=retry_delay,
            enable_cache=enable_cache,
            cache_ttl=cache_ttl
        )
    return _global_executor


def shutdown_global_executor(wait: bool = True) -> None:
    """关闭全局并行执行器"""
    global _global_executor
    if _global_executor is not None:
        _global_executor.shutdown(wait=wait)
        _global_executor = None


def execute_parallel_tasks(
    tasks: List[Tuple[Callable, Tuple, Dict[str, Any]]],
    max_workers: int = 4,
    use_cache: bool = True
) -> Dict[str, Any]:
    """
    并行执行任务（便捷函数）
    
    Args:
        tasks: 任务列表
        max_workers: 最大工作线程数
        use_cache: 是否使用缓存
        
    Returns:
        执行结果
    """
    with ParallelExecutor(max_workers=max_workers, enable_cache=use_cache) as executor:
        results = executor.execute_parallel(tasks, use_cache=use_cache)
        
        # 提取结果
        extracted_results = {}
        for task_id, task_result in results.items():
            if task_result and task_result.success:
                extracted_results[task_id] = task_result.result
            else:
                extracted_results[task_id] = None
        
        return extracted_results