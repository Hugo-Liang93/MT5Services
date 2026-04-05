from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..models import BacktestJob, BacktestJobStatus


_MAX_CACHED_ENTRIES = 50


def _evict_oldest(store: Dict[str, Any], max_size: int = _MAX_CACHED_ENTRIES) -> None:
    """淘汰最早插入的条目，保持字典大小在上限内。"""
    while len(store) > max_size:
        oldest_key = next(iter(store))
        del store[oldest_key]


class BacktestRuntimeStore:
    """回测 API 运行态存储。"""

    def __init__(self, max_entries: int = _MAX_CACHED_ENTRIES) -> None:
        self.jobs: Dict[str, BacktestJob] = {}
        self.results: Dict[str, Any] = {}
        self.walk_forward_results: Dict[str, Any] = {}
        self.recommendations: Dict[str, Any] = {}
        self._max_entries = max_entries

        self.job_lock = threading.Lock()
        self.result_lock = threading.Lock()
        self.walk_forward_lock = threading.Lock()
        self.recommendation_lock = threading.Lock()
        self.semaphore = threading.Semaphore(1)

    def register_job(self, job: BacktestJob) -> None:
        with self.job_lock:
            self.jobs[job.run_id] = job
            _evict_oldest(self.jobs, self._max_entries)

    def list_jobs(self) -> list[BacktestJob]:
        with self.job_lock:
            return list(self.jobs.values())

    def start_job(self, run_id: str) -> None:
        with self.job_lock:
            job = self.jobs.get(run_id)
            if job is not None:
                job.status = BacktestJobStatus.RUNNING
                job.started_at = datetime.now(timezone.utc)

    def complete_job(self, run_id: str, result: Any) -> None:
        now = datetime.now(timezone.utc)
        with self.job_lock:
            job = self.jobs.get(run_id)
            if job is not None:
                job.status = BacktestJobStatus.COMPLETED
                job.completed_at = now
                job.progress = 1.0
        with self.result_lock:
            self.results[run_id] = result
            _evict_oldest(self.results, self._max_entries)

    def fail_job(self, run_id: str, error: str) -> None:
        now = datetime.now(timezone.utc)
        with self.job_lock:
            job = self.jobs.get(run_id)
            if job is not None:
                job.status = BacktestJobStatus.FAILED
                job.completed_at = now
                job.error = error

    def get_runtime_status(self) -> Dict[str, Any]:
        with self.job_lock:
            jobs = list(self.jobs.values())

        pending_jobs = [job for job in jobs if job.status == BacktestJobStatus.PENDING]
        running_jobs = [job for job in jobs if job.status == BacktestJobStatus.RUNNING]
        completed_jobs = [job for job in jobs if job.status == BacktestJobStatus.COMPLETED]
        failed_jobs = [job for job in jobs if job.status == BacktestJobStatus.FAILED]

        latest_job = None
        if jobs:
            latest_job = max(jobs, key=lambda job: job.submitted_at).to_dict()

        with self.result_lock:
            result_cache_size = len(self.results)

        return {
            "pending_jobs": len(pending_jobs),
            "running_jobs": len(running_jobs),
            "completed_jobs": len(completed_jobs),
            "failed_jobs": len(failed_jobs),
            "latest_job": latest_job,
            "result_cache_size": result_cache_size,
        }

    def get_job(self, run_id: str) -> Optional[BacktestJob]:
        with self.job_lock:
            return self.jobs.get(run_id)

    def get_result(self, run_id: str) -> Any:
        with self.result_lock:
            return self.results.get(run_id)

    def set_result(self, run_id: str, result: Any) -> None:
        with self.result_lock:
            self.results[run_id] = result
            _evict_oldest(self.results, self._max_entries)

    def store_walk_forward_result(self, run_id: str, result: Any) -> None:
        with self.walk_forward_lock:
            self.walk_forward_results[run_id] = result
            _evict_oldest(self.walk_forward_results, self._max_entries)

    def get_walk_forward_result(self, run_id: str) -> Any:
        with self.walk_forward_lock:
            return self.walk_forward_results.get(run_id)

    def get_recommendation(self, rec_id: str) -> Any:
        with self.recommendation_lock:
            return self.recommendations.get(rec_id)

    def store_recommendation(self, rec_id: str, recommendation: Any) -> None:
        with self.recommendation_lock:
            self.recommendations[rec_id] = recommendation
            _evict_oldest(self.recommendations, self._max_entries)

    def reset(self) -> None:
        with self.job_lock:
            self.jobs.clear()
        with self.result_lock:
            self.results.clear()
        with self.walk_forward_lock:
            self.walk_forward_results.clear()
        with self.recommendation_lock:
            self.recommendations.clear()


backtest_runtime_store = BacktestRuntimeStore()


def get_backtest_runtime_status() -> Dict[str, Any]:
    return backtest_runtime_store.get_runtime_status()
