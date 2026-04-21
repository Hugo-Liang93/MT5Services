"""DataMatrix 缓存模块（Phase R.1）。

目的：跨 mining run 复用已构建的 DataMatrix，减少重复计算。
设计原则：
  - 输入参数完全相同 → 缓存命中（cache key 由所有入参 hash 决定）
  - cache miss fallback 到原始构建逻辑，零行为变化
  - LRU eviction：超出 max_size_gb 自动淘汰最旧文件
  - TTL：超过 ttl_days 自动失效（默认 30 天，mining 历史数据通常不变）
  - Pickle 加载失败（如 DataMatrix 类签名变更）自动 invalidate

使用：
    from src.research.core.cache import DataMatrixCache, default_cache

    cache = default_cache()
    key = cache.make_key(
        symbol="XAUUSD", timeframe="M5",
        start_time=t0, end_time=t1,
        forward_horizons=[1,3,5,10],
        warmup_bars=200, train_ratio=0.70, ...
    )
    matrix = cache.get(key)
    if matrix is None:
        matrix = build_data_matrix_inner(...)  # 原 build 逻辑
        cache.set(key, matrix)
"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# 默认 cache 路径相对项目根
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "research" / "cache"
# 默认 LRU 上限 5 GB（DataMatrix M5 12mo 约 50-100MB / 个）
_DEFAULT_MAX_SIZE_GB = 5.0
# 默认 TTL 30 天（mining 历史数据通常不变；超期视作不可信）
_DEFAULT_TTL_DAYS = 30


def _to_hashable(value: Any) -> Any:
    """递归把值转为 JSON 兼容的形态，保证序列化稳定。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_hashable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_hashable(v) for k, v in sorted(value.items())}
    if hasattr(value, "_asdict"):  # NamedTuple
        return _to_hashable(value._asdict())
    if hasattr(value, "__dict__"):  # dataclass-like
        return _to_hashable(vars(value))
    # fallback：字符串化
    return str(value)


class DataMatrixCache:
    """文件系统 pickle 缓存 + TTL + LRU eviction。

    线程安全：单进程内调用安全；多进程并发写同一 key 用 atomic rename 保证原子性。
    """

    def __init__(
        self,
        cache_dir: Path = _DEFAULT_CACHE_DIR,
        max_size_gb: float = _DEFAULT_MAX_SIZE_GB,
        ttl_days: int = _DEFAULT_TTL_DAYS,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_size_bytes = int(max_size_gb * 1024**3)
        self.ttl_seconds = int(ttl_days * 86400)

    # ── Key 构造 ────────────────────────────────────────────────────────────

    def make_key(self, **params: Any) -> str:
        """从入参生成确定性 cache key（参数顺序无关）。

        所有 build_data_matrix 入参都应作为关键字参数传入。
        非 JSON 原生类型（datetime / dataclass / tuple）通过 `_to_hashable` 规范化。
        """
        normalized = _to_hashable(params)
        key_str = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]

    # ── Get / Set ───────────────────────────────────────────────────────────

    def _key_path(self, key: str) -> Path:
        return self.cache_dir / f"datamatrix_{key}.pkl"

    def get(self, key: str) -> Optional[Any]:
        """加载缓存。命中且 TTL 未过期 → 返回对象；否则返回 None。

        - 文件不存在 → None
        - TTL 过期 → 删除 + None
        - 加载失败（pickle 错误 / 类签名变化）→ 删除 + None
        """
        path = self._key_path(key)
        if not path.exists():
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        age = time.time() - mtime
        if self.ttl_seconds > 0 and age > self.ttl_seconds:
            try:
                path.unlink()
            except OSError:
                pass
            logger.debug("Cache TTL expired for %s (age=%.0fs)", key[:8], age)
            return None
        try:
            with path.open("rb") as f:
                obj = pickle.load(f)
            # 触摸 mtime 实现 LRU（每次访问更新时间）
            try:
                path.touch()
            except OSError:
                pass
            return obj
        except Exception:
            logger.warning(
                "Cache pickle load failed for %s, invalidating", key[:8], exc_info=True
            )
            try:
                path.unlink()
            except OSError:
                pass
            return None

    def set(self, key: str, value: Any) -> None:
        """写入缓存 + LRU eviction。失败仅 warning，不阻塞调用方。"""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Cache dir mkdir failed: %s", self.cache_dir, exc_info=True)
            return
        path = self._key_path(key)
        tmp_path = path.with_suffix(".pkl.tmp")
        try:
            with tmp_path.open("wb") as f:
                pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
            tmp_path.replace(path)  # atomic rename
        except Exception:
            logger.warning("Cache write failed for %s", key[:8], exc_info=True)
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return
        self._evict_if_needed()

    # ── Maintenance ─────────────────────────────────────────────────────────

    def _evict_if_needed(self) -> None:
        """超出 max_size_bytes 时淘汰最旧（按 mtime 升序）。"""
        if self.max_size_bytes <= 0:
            return
        try:
            files = list(self.cache_dir.glob("datamatrix_*.pkl"))
        except OSError:
            return
        if not files:
            return
        try:
            files_with_meta = [(p, p.stat()) for p in files]
        except OSError:
            return
        total = sum(s.st_size for _, s in files_with_meta)
        if total <= self.max_size_bytes:
            return
        # 按 mtime 升序排序（最旧在前）
        files_with_meta.sort(key=lambda x: x[1].st_mtime)
        evicted = 0
        for path, stat_info in files_with_meta:
            if total <= self.max_size_bytes:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= stat_info.st_size
            evicted += 1
        if evicted:
            logger.info(
                "Cache LRU evicted %d files (now %.1f MB / max %.1f MB)",
                evicted,
                total / (1024**2),
                self.max_size_bytes / (1024**2),
            )

    def clear(self) -> int:
        """清空全部缓存，返回删除文件数。"""
        if not self.cache_dir.exists():
            return 0
        cleared = 0
        for path in self.cache_dir.glob("datamatrix_*.pkl"):
            try:
                path.unlink()
                cleared += 1
            except OSError:
                continue
        return cleared

    def stats(self) -> Mapping[str, Any]:
        """返回 cache 当前规模摘要（文件数 / 总大小 / 最旧 mtime）。"""
        if not self.cache_dir.exists():
            return {"files": 0, "size_bytes": 0, "oldest_mtime": None}
        files = list(self.cache_dir.glob("datamatrix_*.pkl"))
        if not files:
            return {"files": 0, "size_bytes": 0, "oldest_mtime": None}
        try:
            stats_list = [p.stat() for p in files]
        except OSError:
            return {"files": len(files), "size_bytes": 0, "oldest_mtime": None}
        return {
            "files": len(files),
            "size_bytes": sum(s.st_size for s in stats_list),
            "oldest_mtime": min(s.st_mtime for s in stats_list),
        }


# ── 默认单例（按需懒加载） ───────────────────────────────────────────────────

_default_cache: Optional[DataMatrixCache] = None


def default_cache() -> DataMatrixCache:
    """返回项目默认 cache 实例（懒初始化，单例）。"""
    global _default_cache
    if _default_cache is None:
        _default_cache = DataMatrixCache()
    return _default_cache


def hash_indicator_set() -> str:
    """计算当前启用的 indicator 名集合的 hash。

    用于 cache key —— `config/indicators.json` 改变 → indicator 集变 → cache 失效。
    """
    try:
        from src.config.indicator_config import get_global_config_manager

        cfg = get_global_config_manager().get_config()
        names = sorted(ind.name for ind in cfg.indicators)
    except Exception:
        return "unknown"
    return hashlib.sha256("|".join(names).encode()).hexdigest()[:8]
