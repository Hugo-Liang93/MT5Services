"""置信度校准器（ConfidenceCalibrator）。

## 核心问题

当前信号置信度是**静态规则**：
    confidence = min(abs(spread) * 150, 1.0)   # SmaTrendStrategy

这个数字是经验值，不反映该策略在真实市场中的历史胜率。
一个"规则上信心满满"的 0.85 信号，可能实际上只有 45% 的胜率。

## 解决方案：胜率校准

利用 ``OutcomeTracker`` 积累的 ``signal_outcomes`` 数据，
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

import logging
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
        推荐从保守值（0.3）开始，观察效果后调整。
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
        alpha: float = 0.30,
        baseline_win_rate: float = 0.50,
        max_boost: float = 1.30,
        min_samples: int = 20,
        refresh_interval_seconds: int = 3600,
        recency_hours: int = 48,
    ) -> None:
        if not (0.0 <= alpha <= 1.0):
            raise ValueError(f"alpha must be in [0.0, 1.0], got {alpha}")
        self._fetch_fn = fetch_winrates_fn
        self._alpha = alpha
        self._baseline = baseline_win_rate
        self._max_boost = max_boost
        self._min_samples = min_samples
        self._refresh_interval = refresh_interval_seconds
        # 近期窗口（小时）：刷新时额外拉取短窗口胜率，用于防止正反馈。
        # 若近期（recency_hours 内）胜率低于 baseline，则禁止放大置信度（factor 上限=1.0）。
        # 短窗口样本不足时自动退化为不校准（不影响历史窗口的压制效果）。
        self._recency_hours: int = max(1, recency_hours)

        # { (strategy, action, regime_value): (win_rate, sample_count) }
        self._cache: Dict[_WinRateKey, Tuple[float, int]] = {}
        # 近期窗口缓存，结构与 _cache 相同
        self._recent_cache: Dict[_WinRateKey, Tuple[float, int]] = {}
        self._cache_lock = threading.Lock()
        self._last_refresh: float = 0.0
        self._refresh_hours: int = 168   # 查询最近 7 天的历史

        # 统计：校准了多少次、调整了多少
        self._total_calibrated: int = 0
        self._total_boosted: int = 0
        self._total_suppressed: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calibrate(
        self,
        strategy: str,
        action: str,
        raw_confidence: float,
        regime: RegimeType,
    ) -> float:
        """返回校准后的置信度（始终 ∈ [0.0, 1.0]）。

        如果没有足够的历史数据，直接返回 raw_confidence（无影响）。
        """
        if self._alpha < 1e-6:
            return raw_confidence  # alpha=0：完全不校准

        self._auto_refresh()
        factor = self._get_calibration_factor(strategy, action, regime)
        if factor is None:
            return raw_confidence  # 无数据或样本不足 → 不干预

        # 混合校准：加权平均原始置信度和历史校准值
        calibrated = raw_confidence * (1.0 - self._alpha) + raw_confidence * factor * self._alpha
        result = max(0.0, min(1.0, calibrated))

        # 统计
        self._total_calibrated += 1
        if result > raw_confidence + 1e-4:
            self._total_boosted += 1
        elif result < raw_confidence - 1e-4:
            self._total_suppressed += 1

        return result

    def refresh(self, *, symbol: Optional[str] = None) -> int:
        """立即刷新胜率缓存（历史窗口 + 近期窗口），返回加载的记录数。"""
        try:
            rows = self._fetch_fn(hours=self._refresh_hours, symbol=symbol)
        except Exception:
            logger.exception("ConfidenceCalibrator: failed to fetch win rates")
            return 0

        new_cache: Dict[_WinRateKey, Tuple[float, int]] = {}
        count = 0
        for row in rows:
            # row: (strategy, action, total, wins, win_rate, avg_conf, avg_move)
            strat = str(row[0])
            act = str(row[1])
            total = int(row[2]) if row[2] is not None else 0
            win_rate = float(row[4]) if row[4] is not None else 0.0
            # regime 为 None 表示全局（不分 Regime 的聚合行）
            regime_val = str(row[7]) if len(row) > 7 and row[7] is not None else "_all"
            if total >= self._min_samples:
                new_cache[(strat, act, regime_val)] = (win_rate, total)
                count += 1

        # ── 近期窗口：用于防止正反馈 ─────────────────────────────────
        # 若近期（recency_hours 内）胜率低于 baseline，
        # _get_calibration_factor 会将 boost 上限夹到 1.0，
        # 避免在行情转折点还继续放大置信度。
        new_recent_cache: Dict[_WinRateKey, Tuple[float, int]] = {}
        try:
            recent_rows = self._fetch_fn(hours=self._recency_hours, symbol=symbol)
            for row in recent_rows:
                strat = str(row[0])
                act = str(row[1])
                total = int(row[2]) if row[2] is not None else 0
                win_rate = float(row[4]) if row[4] is not None else 0.0
                regime_val = str(row[7]) if len(row) > 7 and row[7] is not None else "_all"
                # 近期窗口样本量要求减半（宽松），优先保证覆盖率
                if total >= max(1, self._min_samples // 2):
                    new_recent_cache[(strat, act, regime_val)] = (win_rate, total)
        except Exception:
            logger.warning("ConfidenceCalibrator: failed to fetch recent win rates", exc_info=True)

        with self._cache_lock:
            self._cache = new_cache
            self._recent_cache = new_recent_cache
            self._last_refresh = time.monotonic()

        logger.info(
            "ConfidenceCalibrator: refreshed %d win-rate entries (%d recent)",
            count, len(new_recent_cache),
        )
        return count

    def describe(self) -> Dict[str, Any]:
        """返回当前状态，用于监控端点。"""
        with self._cache_lock:
            cache_size = len(self._cache)
            recent_cache_size = len(self._recent_cache)
            age_seconds = time.monotonic() - self._last_refresh if self._last_refresh else None
        return {
            "alpha": self._alpha,
            "baseline_win_rate": self._baseline,
            "max_boost": self._max_boost,
            "min_samples": self._min_samples,
            "recency_hours": self._recency_hours,
            "cache_entries": cache_size,
            "recent_cache_entries": recent_cache_size,
            "cache_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "refresh_interval_seconds": self._refresh_interval,
            "stats": {
                "total_calibrated": self._total_calibrated,
                "total_boosted": self._total_boosted,
                "total_suppressed": self._total_suppressed,
            },
        }

    # ------------------------------------------------------------------
    # Protected helpers（ML 阶段可子类化覆盖此方法）
    # ------------------------------------------------------------------

    def _get_calibration_factor(
        self,
        strategy: str,
        action: str,
        regime: RegimeType,
    ) -> Optional[float]:
        """返回置信度校准因子，None 表示样本不足不干预。

        当前实现：基于历史胜率，并通过近期窗口防止正反馈。
        ML 阶段：子类可覆盖此方法，调用模型推理。

        正反馈保护：
            若 factor > 1.0（准备放大置信度），同时检查近期（recency_hours）胜率。
            若近期胜率低于 baseline（策略近期表现下滑），将 factor 夹到 1.0，
            仅允许压制，不允许放大，避免在行情转折点过度乐观。
        """
        regime_val = regime.value
        with self._cache_lock:
            # 优先查找 regime 细化版本
            entry = self._cache.get((strategy, action, regime_val))
            if entry is None:
                # 回退到全局聚合（没有 regime 分类的旧数据）
                entry = self._cache.get((strategy, action, "_all"))
            # 读取近期缓存（与 cache 同一把锁）
            recent_entry = self._recent_cache.get((strategy, action, regime_val))
            if recent_entry is None:
                recent_entry = self._recent_cache.get((strategy, action, "_all"))

        if entry is None:
            return None

        win_rate, _samples = entry
        # factor = win_rate / baseline（factor>1 提升，factor<1 压制）
        factor = win_rate / max(self._baseline, 1e-6)
        # 限制最大提升倍数，防止极端值
        factor = min(factor, self._max_boost)

        # ── 正反馈保护：近期胜率下滑时禁止 boost ────────────────────
        # 即使历史 7 天整体胜率不错，若近期 recency_hours 内胜率已低于基准，
        # 说明策略处于下行周期，不应再放大置信度（仅保留压制能力）。
        if factor > 1.0 and recent_entry is not None:
            recent_win_rate, _recent_samples = recent_entry
            if recent_win_rate < self._baseline:
                factor = 1.0  # 近期不达标：禁止 boost，不压制

        return factor

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _auto_refresh(self) -> None:
        """若缓存已过期则在调用线程中自动刷新（非阻塞检查）。"""
        now = time.monotonic()
        if now - self._last_refresh >= self._refresh_interval:
            self.refresh()
