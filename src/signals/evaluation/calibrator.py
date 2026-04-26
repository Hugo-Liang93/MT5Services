"""置信度校准器（ConfidenceCalibrator）。

## 核心问题

当前信号置信度是**静态规则**：
    confidence = min(abs(spread) * 150, 1.0)   # SmaTrendStrategy

这个数字是经验值，不反映该策略在真实市场中的历史胜率。
一个"规则上信心满满"的 0.85 信号，可能实际上只有 45% 的胜率。

## 解决方案：胜率校准

利用 ``SignalQualityTracker`` 积累的 ``signal_outcomes`` 数据，
计算每个 ``(strategy, action, regime)`` 的历史胜率，
对原始置信度进行**混合校准**：

    adjusted = raw * (1 - alpha) + raw * calibration_factor * alpha

    calibration_factor = win_rate / baseline_win_rate
        win_rate < baseline  →  factor < 1.0  →  压制置信度
        win_rate > baseline  →  factor > 1.0  →  适当提升
        win_rate = baseline  →  factor = 1.0  →  不变

alpha（混合系数）控制历史数据对当前信号的影响程度：
    alpha = 0.0  →  完全不校准（纯规则）
    alpha = 0.5  →  规则与历史各占一半
    alpha = 1.0  →  完全由历史决定

## 为什么分 Regime 校准

同一策略在不同行情类型下的胜率差异极大：
    SmaTrendStrategy: TRENDING win_rate=0.72, RANGING win_rate=0.31

分 Regime 计算，可以更精准地压制"不适合当前行情"的策略信号，
而不是用一个全局胜率拉平这种差异（Regime 亲和度已在 service.py 中处理，
但校准是对实际结果的反馈，两者是互补的）。

## 样本量保护

当某个 (strategy, action, regime) 的历史样本 < min_samples 时，
校准因子设为 1.0（不干预），避免基于噪声数据做出错误调整。

## 机器学习接入预留

``calibrate()`` 的签名 (strategy, action, raw_confidence, regime) → float
在 ML 阶段可以通过子类覆盖 ``_get_calibration_factor()``，
将规则替换为模型预测，接口对 SignalModule 完全透明。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .regime import RegimeType

logger = logging.getLogger(__name__)

# (strategy_name, action, regime_value) → win_rate
_WinRateKey = Tuple[str, str, str]


class ConfidenceCalibrator:
    """基于历史胜率对策略原始置信度进行混合校准。

    参数
    ----
    fetch_winrates_fn:
        接受 ``(hours, symbol=None)`` 并返回
        ``[(strategy, action, total, wins, win_rate, avg_conf, avg_move), ...]``
        的回调（对应 ``TimescaleWriter.fetch_winrates``）。
    alpha:
        历史数据权重（0.0 = 不校准，1.0 = 完全由历史决定）。
        推荐从更轻的混合值（0.15）开始，减少历史样本对快速日内行情的滞后影响。
    baseline_win_rate:
        校准基准线（factor=1.0 时对应的胜率）。
        默认 0.50（随机基准）；若策略设计本就偏保守，可设为 0.55。
    max_boost:
        历史胜率优秀时置信度的最大提升倍数（防止过度乐观）。
    min_samples:
        样本量不足时不校准（返回原始置信度），避免噪声干扰。
    refresh_interval_seconds:
        后台自动刷新胜率缓存的间隔（默认每小时）。
        手动调用 ``refresh()`` 可立即触发。
    """

    def __init__(
        self,
        fetch_winrates_fn: Callable[..., Any],
        *,
        alpha: float = 0.15,
        baseline_win_rate: float = 0.50,
        max_boost: float = 1.30,
        min_samples: int = 100,
        refresh_interval_seconds: int = 3600,
        recency_hours: int = 8,
        warmup_alpha: float = 0.10,
        full_alpha_min_samples: int = 200,
    ) -> None:
        if not (0.0 <= alpha <= 1.0):
            raise ValueError(f"alpha must be in [0.0, 1.0], got {alpha}")
        self._fetch_fn = fetch_winrates_fn
        self._alpha = alpha
        self._baseline = baseline_win_rate
        self._max_boost = max_boost
        self._min_samples = min_samples
        self._warmup_alpha = max(0.0, min(alpha, warmup_alpha))
        self._full_alpha_min_samples = max(min_samples, full_alpha_min_samples)
        self._refresh_interval = refresh_interval_seconds
        # 近期窗口（小时）：刷新时额外拉取短窗口胜率，用于防止正反馈。
        # 按 TF 差异化：高 TF 的 bar 间隔大，需要更长窗口才有足够样本。
        self._recency_hours: int = max(1, recency_hours)
        self._recency_hours_by_tf: dict[str, int] = {
            "M1": 4,
            "M5": 8,
            "M15": 12,
            "M30": 16,
            "H1": 24,
            "H4": 72,
            "D1": 168,
        }
        # refresh 时拉取的窗口集合（去重 + 排序）
        self._recency_windows: tuple[int, ...] = tuple(
            sorted(set(self._recency_hours_by_tf.values()))
        )

        # { (strategy, action, regime_value): (win_rate, sample_count) }
        self._cache: Dict[_WinRateKey, Tuple[float, int]] = {}
        # 近期窗口缓存：{ hours: { (strategy, action, regime_value): (win_rate, sample_count) } }
        self._recent_caches: Dict[int, Dict[_WinRateKey, Tuple[float, int]]] = {}
        # 作为默认窗口回退的基准缓存，服务 describe() 和默认路径读取。
        self._default_recent_cache: Dict[_WinRateKey, Tuple[float, int]] = {}
        # 写锁：仅用于 refresh/dump/load 等写操作，保护 _cache/_default_recent_cache 的整体替换
        self._cache_lock = threading.Lock()
        # 统计锁：轻量级，仅保护计数器更新，避免与写锁竞争
        self._stats_lock = threading.Lock()
        self._last_refresh: float = 0.0
        self._refresh_hours: int = 168  # 查询最近 7 天的历史

        # 统计：校准了多少次、调整了多少
        self._total_calibrated: int = 0
        self._total_boosted: int = 0
        self._total_suppressed: int = 0

        # C-1/C-2: 后台刷新线程
        self._bg_thread: Optional[threading.Thread] = None
        self._bg_stop: threading.Event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_recency_config(
        self,
        recency_hours: int | None = None,
        hours_by_tf: dict[str, int] | None = None,
    ) -> None:
        """热更新近期窗口配置（供装配层和热重载使用）。"""
        if recency_hours is not None:
            self._recency_hours = max(1, recency_hours)
        if hours_by_tf:
            self._recency_hours_by_tf.update(hours_by_tf)
        self._recency_windows = tuple(sorted(set(self._recency_hours_by_tf.values())))

    def start_background_refresh(self, *, symbol: Optional[str] = None) -> None:
        """启动后台刷新线程，定期更新胜率缓存（非阻塞）。

        调用后 calibrate() 读取缓存时不再需要等待 DB 查询。
        若已有后台线程在运行，此调用无操作。
        """
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return
        self._bg_stop.clear()
        self._bg_thread = threading.Thread(
            target=self._bg_loop,
            args=(symbol,),
            name="calibrator-refresh",
            daemon=False,
        )
        self._bg_thread.start()
        logger.info(
            "ConfidenceCalibrator: background refresh started (interval=%ds)",
            self._refresh_interval,
        )

    def stop_background_refresh(self) -> None:
        """停止后台刷新线程（最多等待 10 秒）。

        ADR-005：join(timeout) 后必须 is_alive() 检查；线程因 I/O / 队列等待 /
        外部依赖卡住仍 alive 时**保留引用**，避免 start_background_refresh 的
        防重逻辑（仅查 _bg_thread is not None and is_alive()）误判已停 →
        启动第二条刷新线程造成双线程消费 / 重复缓存写入。
        """
        self._bg_stop.set()
        if self._bg_thread is not None:
            self._bg_thread.join(timeout=10.0)
            if self._bg_thread.is_alive():
                logger.warning(
                    "ConfidenceCalibrator: bg refresh thread still alive after"
                    " 10s join timeout; preserving reference per ADR-005"
                )
            else:
                self._bg_thread = None
        logger.info("ConfidenceCalibrator: background refresh stopped")

    def calibrate(
        self,
        strategy: str,
        action: str,
        raw_confidence: float,
        regime: RegimeType,
        timeframe: str | None = None,
    ) -> float:
        """返回校准后的置信度（始终 ∈ [0.0, 1.0]）。

        如果没有足够的历史数据，直接返回 raw_confidence（无影响）。
        """
        if self._alpha < 1e-6:
            return raw_confidence  # alpha=0：完全不校准

        # C-1: 热路径中不再调用 _auto_refresh()（已由后台线程接管）。
        # 若未启动后台线程，在首次调用时执行一次前台刷新，确保采样可用。
        if self._bg_thread is None or not self._bg_thread.is_alive():
            self._auto_refresh()
        factor, sample_count = self._get_calibration_factor(
            strategy, action, regime, timeframe=timeframe
        )
        if factor is None:
            return raw_confidence  # 无数据或样本不足 → 不干预
        effective_alpha = self._resolve_alpha(sample_count)
        if effective_alpha < 1e-6:
            return raw_confidence

        # 简化校准公式：raw * (1 + (factor - 1) * alpha)
        calibrated = raw_confidence * (1.0 + (factor - 1.0) * effective_alpha)
        result = max(0.0, min(1.0, calibrated))

        # 统计（使用独立的轻量锁，避免与 refresh 写锁竞争）
        with self._stats_lock:
            self._total_calibrated += 1
            if result > raw_confidence + 1e-4:
                self._total_boosted += 1
            elif result < raw_confidence - 1e-4:
                self._total_suppressed += 1

        return result

    def refresh(
        self,
        *,
        symbol: Optional[str] = None,
        hours: Optional[int] = None,
    ) -> int:
        """立即刷新胜率缓存（历史窗口 + 近期窗口），返回加载的记录数。

        §0y P2：``hours`` 是**一次性**主窗口覆盖（不修改 self._refresh_hours）。
        旧 API ``calibrator._refresh_hours = h`` + ``refresh()`` 把后续后台
        刷新和默认 refresh 主窗口都永久改成 h；正确做法是显式 one-shot 参数，
        让"本次按 h 刷新"与"全局默认 refresh 窗口"语义分开。
        """
        effective_hours = int(hours) if hours is not None else self._refresh_hours
        try:
            rows = self._fetch_fn(hours=effective_hours, symbol=symbol)
        except Exception:
            logger.exception("ConfidenceCalibrator: failed to fetch win rates")
            return 0

        new_cache: Dict[_WinRateKey, Tuple[float, int]] = {}
        count = 0
        for row in rows:
            # row: (strategy, action, total, wins, win_rate, avg_conf, avg_move, regime)
            strat = str(row[0])
            act = str(row[1])
            total = int(row[2]) if row[2] is not None else 0
            win_rate = float(row[4]) if row[4] is not None else 0.0
            # regime 列（第 8 列，index=7）：SQL 现在按 regime 分组
            regime_val = str(row[7]) if len(row) > 7 and row[7] is not None else "_all"
            if total >= self._min_samples:
                new_cache[(strat, act, regime_val)] = (win_rate, total)
                count += 1

        # ── 近期窗口（per-TF 差异化）：用于防止正反馈 ────────────────
        # 为每个配置的时间窗口分别拉取近期胜率。calibrate() 时按 timeframe
        # 查表选择对应窗口，高 TF 使用更长窗口以获得足够样本。
        new_recent_caches: Dict[int, Dict[_WinRateKey, Tuple[float, int]]] = {}
        min_recent_samples = max(1, self._min_samples // 2)
        for window_hours in self._recency_windows:
            window_cache: Dict[_WinRateKey, Tuple[float, int]] = {}
            try:
                recent_rows = self._fetch_fn(hours=window_hours, symbol=symbol)
                for row in recent_rows:
                    strat = str(row[0])
                    act = str(row[1])
                    total = int(row[2]) if row[2] is not None else 0
                    win_rate = float(row[4]) if row[4] is not None else 0.0
                    regime_val = (
                        str(row[7]) if len(row) > 7 and row[7] is not None else "_all"
                    )
                    if total >= min_recent_samples:
                        window_cache[(strat, act, regime_val)] = (win_rate, total)
            except Exception:
                logger.warning(
                    "ConfidenceCalibrator: failed to fetch recent win rates (window=%dh)",
                    window_hours,
                    exc_info=True,
                )
            new_recent_caches[window_hours] = window_cache

        # 默认窗口用于 describe() 与 _get_calibration_factor 的回退读取
        default_recent = new_recent_caches.get(self._recency_hours, {})
        total_recent = sum(len(c) for c in new_recent_caches.values())

        with self._cache_lock:
            self._cache = new_cache
            self._recent_caches = new_recent_caches
            self._default_recent_cache = default_recent
            self._last_refresh = time.monotonic()

        logger.info(
            "ConfidenceCalibrator: refreshed %d win-rate entries (%d recent across %d windows)",
            count,
            total_recent,
            len(self._recency_windows),
        )
        return count

    def describe(self) -> Dict[str, Any]:
        """返回当前状态，用于监控端点。"""
        # 快照读取：无需加写锁，len() 和属性读取在 CPython 中是原子的
        cache_size = len(self._cache)
        recent_cache_size = len(self._default_recent_cache)
        last_refresh = self._last_refresh
        age_seconds = time.monotonic() - last_refresh if last_refresh else None
        with self._stats_lock:
            stats = {
                "total_calibrated": self._total_calibrated,
                "total_boosted": self._total_boosted,
                "total_suppressed": self._total_suppressed,
            }
        return {
            "alpha": self._alpha,
            "warmup_alpha": self._warmup_alpha,
            "full_alpha_min_samples": self._full_alpha_min_samples,
            "baseline_win_rate": self._baseline,
            "max_boost": self._max_boost,
            "min_samples": self._min_samples,
            "recency_hours": self._recency_hours,
            # §0y P2：暴露默认主窗口给消费侧（API / dashboard 可看出全局配置，
            # 一次性 refresh(hours=) 不改这里的值）
            "refresh_hours": self._refresh_hours,
            "cache_entries": cache_size,
            "recent_cache_entries": recent_cache_size,
            "cache_age_seconds": (
                round(age_seconds, 1) if age_seconds is not None else None
            ),
            "refresh_interval_seconds": self._refresh_interval,
            "stats": stats,
        }

    def dump(self, path: str) -> None:
        """C-3: 将当前胜率缓存持久化到 JSON 文件，重启后可快速恢复而无需等待 DB。

        格式：``{"timestamp": ..., "cache": {"strat|act|regime": {"win_rate": 0.6, "total": 50}, ...}}``
        """
        # 快照读取当前缓存引用，无需持有写锁（dict 遍历在 CPython 中是安全的）
        cache_snapshot = self._cache
        data = {
            "timestamp": time.time(),
            "cache": {
                f"{k[0]}|{k[1]}|{k[2]}": {"win_rate": v[0], "total": v[1]}
                for k, v in cache_snapshot.items()
            },
        }
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(data, fp, indent=2)
            logger.info(
                "ConfidenceCalibrator: dumped %d cache entries to %s",
                len(data["cache"]),
                path,
            )
        except Exception:
            logger.warning(
                "ConfidenceCalibrator: failed to dump cache to %s", path, exc_info=True
            )

    def load(self, path: str) -> int:
        """C-3: 从 JSON 文件加载胜率缓存，合并到现有缓存（不替换）。

        返回成功加载的条目数。文件不存在或格式错误时安全退出（返回 0）。
        """
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            new_entries: Dict[_WinRateKey, Tuple[float, int]] = {}
            for key_str, entry in data.get("cache", {}).items():
                parts = key_str.split("|", 2)
                if len(parts) == 3:
                    try:
                        new_entries[(parts[0], parts[1], parts[2])] = (
                            float(entry["win_rate"]),
                            int(entry["total"]),
                        )
                    except (KeyError, TypeError, ValueError):
                        pass
            with self._cache_lock:
                # 合并：保留 DB 刷新的数据，仅补充文件中的缺失条目
                for k, v in new_entries.items():
                    self._cache.setdefault(k, v)
            logger.info(
                "ConfidenceCalibrator: loaded %d cache entries from %s",
                len(new_entries),
                path,
            )
            return len(new_entries)
        except Exception:
            logger.warning(
                "ConfidenceCalibrator: failed to load cache from %s",
                path,
                exc_info=True,
            )
            return 0

    # ------------------------------------------------------------------
    # Protected helpers（ML 阶段可子类化覆盖此方法）
    # ------------------------------------------------------------------

    def _get_calibration_factor(
        self,
        strategy: str,
        action: str,
        regime: RegimeType,
        *,
        timeframe: str | None = None,
    ) -> tuple[Optional[float], int]:
        """返回 (校准因子, 样本量)，None 表示样本不足不干预。

        正反馈保护按 timeframe 选择近期窗口：高 TF 用更长窗口以获得足够样本。
        """
        regime_val = regime.value
        cache = self._cache
        # 按 TF 选择近期窗口
        recency_hours = self._recency_hours_by_tf.get(
            timeframe or "", self._recency_hours
        )
        recent_cache = self._recent_caches.get(
            recency_hours, self._default_recent_cache
        )

        entry = cache.get((strategy, action, regime_val))
        if entry is None:
            entry = cache.get((strategy, action, "_all"))
        recent_entry = recent_cache.get((strategy, action, regime_val))
        if recent_entry is None:
            recent_entry = recent_cache.get((strategy, action, "_all"))

        if entry is None:
            return None, 0

        win_rate, samples = entry
        factor = win_rate / max(self._baseline, 1e-6)
        factor = min(factor, self._max_boost)

        # 正反馈保护：近期胜率下滑时禁止 boost
        if factor > 1.0 and recent_entry is not None:
            recent_win_rate, _recent_samples = recent_entry
            if recent_win_rate < self._baseline:
                factor = 1.0

        return factor, samples

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _bg_loop(self, symbol: Optional[str]) -> None:
        """C-1/C-2: 后台刷新循环 — 立即执行首次刷新，之后每隔 refresh_interval 再刷新。"""
        while not self._bg_stop.is_set():
            try:
                self.refresh(symbol=symbol)
            except Exception:
                logger.exception(
                    "ConfidenceCalibrator: background refresh iteration failed"
                )
            self._bg_stop.wait(timeout=self._refresh_interval)

    def _auto_refresh(self) -> None:
        """若缓存已过期则在调用线程中前台刷新。"""
        now = time.monotonic()
        if now - self._last_refresh >= self._refresh_interval:
            self.refresh()

    def _resolve_alpha(self, sample_count: int) -> float:
        if sample_count < self._min_samples:
            return 0.0
        if sample_count < self._full_alpha_min_samples:
            return self._warmup_alpha
        return self._alpha
