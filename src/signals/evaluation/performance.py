"""日内策略绩效追踪器（StrategyPerformanceTracker）。

## 核心问题

ConfidenceCalibrator 需要 DB + 50 个历史样本才能生效，
对当天日内交易的反馈太慢。策略可能在当天连续亏损，
但 calibrator 仍然基于 7 天历史数据给出 boost。

## 解决方案

StrategyPerformanceTracker 是纯内存的日内实时反馈层：
  - 接收 OutcomeTracker / PositionManager 的结果回调
  - 维护 per-strategy 滚动统计（wins, losses, streak, PnL）
  - 维护 per-(strategy, regime) 维度统计，支持按当前 regime 查询乘数
  - 提供 get_multiplier(strategy, regime=None) → [min_multiplier, max_multiplier]
  - Session 边界自动重置

## 与 ConfidenceCalibrator 的关系

两者互补，在置信度流水线中串联：
    raw_confidence
      x regime_affinity         (结构性过滤)
      x session_performance     (日内实时状态)     ← PerformanceTracker
      → ConfidenceCalibrator    (长期统计校准)     ← Calibrator
      = final_confidence

## 乘数计算逻辑

基于三个因素加权得到最终乘数：

1. **session_win_rate vs baseline**:
   - win_rate > baseline → 微幅提升
   - win_rate < baseline → 压制

2. **streak（连胜/连败）**:
   - 连败 >= streak_penalty_threshold → 额外压制
   - 连胜 >= streak_boost_threshold → 无额外提升（避免过度乐观）

3. **profit_factor**（avg_win / avg_loss）:
   - < 1.0 → 亏损策略，压制
   - > 1.5 → 盈利策略，微幅提升

## Category 聚合

当单策略样本不足（< category_fallback_min_samples）时，
回退到同类策略的聚合绩效，避免冷启动问题。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class _StrategyStats:
    """单个策略的日内滚动统计。"""

    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_win_pnl: float = 0.0
    total_loss_pnl: float = 0.0
    current_streak: int = 0  # >0 连胜, <0 连败
    max_streak: int = 0
    min_streak: int = 0
    last_outcome_at: float = 0.0  # monotonic

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> Optional[float]:
        if self.total == 0:
            return None
        return self.wins / self.total

    @property
    def avg_win(self) -> Optional[float]:
        if self.wins == 0:
            return None
        return self.total_win_pnl / self.wins

    @property
    def avg_loss(self) -> Optional[float]:
        if self.losses == 0:
            return None
        return self.total_loss_pnl / self.losses

    @property
    def profit_factor(self) -> Optional[float]:
        if self.total_loss_pnl == 0:
            return None
        if self.total_win_pnl == 0:
            return 0.0
        return abs(self.total_win_pnl / self.total_loss_pnl)

    def record(self, won: bool, pnl: float) -> None:
        # NaN/Inf 防守：防止污染统计数据
        if not math.isfinite(pnl):
            pnl = 0.0
        if won:
            self.wins += 1
            self.total_win_pnl += pnl
            self.current_streak = max(1, self.current_streak + 1)
        else:
            self.losses += 1
            self.total_loss_pnl += pnl
            self.current_streak = min(-1, self.current_streak - 1)
        self.total_pnl += pnl
        self.max_streak = max(self.max_streak, self.current_streak)
        self.min_streak = min(self.min_streak, self.current_streak)
        self.last_outcome_at = time.monotonic()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "wins": self.wins,
            "losses": self.losses,
            "total": self.total,
            "win_rate": round(self.win_rate, 4) if self.win_rate is not None else None,
            "total_pnl": round(self.total_pnl, 4),
            "profit_factor": (
                round(self.profit_factor, 4)
                if self.profit_factor is not None
                else None
            ),
            "current_streak": self.current_streak,
            "max_streak": self.max_streak,
            "min_streak": self.min_streak,
        }


@dataclass(frozen=True)
class PerformanceTrackerConfig:
    """PerformanceTracker 配置参数。

    enabled:
        是否启用日内绩效追踪（False = get_multiplier 始终返回 1.0）。
    baseline_win_rate:
        基准胜率（默认 0.50）。高于此值时微幅提升，低于时压制。
    min_multiplier:
        乘数下限（默认 0.50），防止单策略被过度压制。
    max_multiplier:
        乘数上限（默认 1.20），防止过度乐观。
    streak_penalty_threshold:
        连败达到此值后触发额外压制（默认 3 次）。
    streak_penalty_factor:
        连败额外压制因子（默认 0.90）：每超过阈值 1 次，乘以此值。
    category_fallback_min_samples:
        单策略样本不足此值时，回退到同类策略的聚合绩效。
    session_reset_interval_hours:
        Session 重置间隔（小时），0 = 不自动重置。
    """

    enabled: bool = True
    baseline_win_rate: float = 0.50
    min_multiplier: float = 0.50
    max_multiplier: float = 1.20
    streak_penalty_threshold: int = 3
    streak_penalty_factor: float = 0.90
    category_fallback_min_samples: int = 3
    session_reset_interval_hours: int = 0


class StrategyPerformanceTracker:
    """日内策略绩效追踪器 — 实时反馈层。

    线程安全：所有读写通过 _lock 保护。
    """

    def __init__(
        self,
        config: Optional[PerformanceTrackerConfig] = None,
        *,
        strategy_categories: Optional[Dict[str, str]] = None,
    ) -> None:
        self._config = config or PerformanceTrackerConfig()
        # strategy_name → category_name（在 SignalModule 注册时填充）
        self._strategy_categories: Dict[str, str] = dict(strategy_categories or {})
        self._lock = threading.RLock()
        # per-strategy stats
        self._stats: Dict[str, _StrategyStats] = {}
        # per-(strategy, regime) stats — regime 维度的细分统计
        self._regime_stats: Dict[Tuple[str, str], _StrategyStats] = {}
        # per-category aggregate stats
        self._category_stats: Dict[str, _StrategyStats] = {}
        self._session_start_at: float = time.monotonic()
        self._total_recorded: int = 0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def register_strategy(self, strategy_name: str, category: str) -> None:
        """注册策略与其分类的映射关系。启动时由 SignalModule 调用。"""
        with self._lock:
            self._strategy_categories[strategy_name] = category

    # ------------------------------------------------------------------
    # Data input
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        strategy: str,
        won: bool,
        pnl: float = 0.0,
        *,
        action: Optional[str] = None,
        regime: Optional[str] = None,
    ) -> None:
        """记录一次策略评估结果。

        由 OutcomeTracker 回调或 PositionManager 关仓时调用。

        参数
        ----
        strategy: 策略名称
        won: 是否盈利
        pnl: 盈亏金额（可选，用于 profit_factor 计算）
        action: buy/sell（预留，当前未使用）
        regime: 信号发出时的市场 regime（用于 per-regime 维度统计）
        """
        if not self._config.enabled:
            return

        with self._lock:
            # 按策略记录
            stats = self._stats.setdefault(strategy, _StrategyStats())
            stats.record(won, pnl)

            # 按 (strategy, regime) 记录
            if regime:
                regime_key = (strategy, regime)
                regime_stats = self._regime_stats.setdefault(
                    regime_key, _StrategyStats()
                )
                regime_stats.record(won, pnl)

            # 按分类聚合
            category = self._strategy_categories.get(strategy, "unknown")
            cat_stats = self._category_stats.setdefault(category, _StrategyStats())
            cat_stats.record(won, pnl)

            self._total_recorded += 1

    # ------------------------------------------------------------------
    # Multiplier calculation
    # ------------------------------------------------------------------

    def get_multiplier(
        self, strategy: str, *, regime: Optional[str] = None
    ) -> float:
        """返回该策略的日内绩效乘数 [min_multiplier, max_multiplier]。

        未启用或无数据时返回 1.0（无影响）。

        当传入 regime 时，优先使用 per-(strategy, regime) 维度的统计数据。
        若 regime 维度样本不足，与全局策略统计混合，避免稀疏数据下的过拟合。
        """
        if not self._config.enabled:
            return 1.0

        with self._lock:
            # 优先尝试 regime 维度统计
            regime_mult = self._regime_aware_multiplier(strategy, regime)
            if regime_mult is not None:
                return regime_mult

            # 回退到全局策略统计
            stats = self._stats.get(strategy)
            if stats is None or stats.total == 0:
                return self._category_fallback_multiplier(strategy)

            if stats.total < self._config.category_fallback_min_samples:
                individual = self._compute_multiplier(stats)
                cat_mult = self._category_fallback_multiplier(strategy)
                weight = (stats.total / self._config.category_fallback_min_samples) ** 0.5
                return individual * weight + cat_mult * (1.0 - weight)

            return self._compute_multiplier(stats)

    def _regime_aware_multiplier(
        self, strategy: str, regime: Optional[str]
    ) -> Optional[float]:
        """尝试使用 regime 维度统计计算乘数。

        返回 None 表示 regime 维度无可用数据，调用者应回退到全局统计。

        混合策略：当 regime 维度样本 >= category_fallback_min_samples 时，
        以 regime 维度为主（权重 0.7）与全局统计（权重 0.3）混合，
        既利用 regime 细分信息，又保留全局统计的稳定性。
        样本不足时按比例降低 regime 维度的权重。
        """
        if not regime:
            return None

        regime_key = (strategy, regime)
        regime_stats = self._regime_stats.get(regime_key)
        if regime_stats is None or regime_stats.total == 0:
            return None

        global_stats = self._stats.get(strategy)
        regime_mult = self._compute_multiplier(regime_stats)

        min_samples = self._config.category_fallback_min_samples
        if regime_stats.total >= min_samples:
            # regime 维度样本充足：regime 0.7 + global 0.3
            if global_stats is not None and global_stats.total > 0:
                global_mult = self._compute_multiplier(global_stats)
                return regime_mult * 0.7 + global_mult * 0.3
            return regime_mult

        # regime 维度样本不足：按比例混合
        # weight 从 0 渐进到 0.7（达到 min_samples 时为 0.7）
        weight = (regime_stats.total / min_samples) ** 0.5 * 0.7
        if global_stats is not None and global_stats.total > 0:
            global_mult = self._compute_multiplier(global_stats)
            return regime_mult * weight + global_mult * (1.0 - weight)

        # 无全局统计，尝试 category fallback
        cat_mult = self._category_fallback_multiplier(strategy)
        return regime_mult * weight + cat_mult * (1.0 - weight)

    def _category_fallback_multiplier(self, strategy: str) -> float:
        """使用同类策略的聚合绩效计算乘数（冷启动回退）。"""
        category = self._strategy_categories.get(strategy, "unknown")
        cat_stats = self._category_stats.get(category)
        if cat_stats is None or cat_stats.total == 0:
            return 1.0
        return self._compute_multiplier(cat_stats)

    def _compute_multiplier(self, stats: _StrategyStats) -> float:
        """核心乘数计算。

        三个因素：
        1. win_rate vs baseline → 基础乘数
        2. 连败惩罚 → 额外折扣
        3. profit_factor → 微调
        """
        cfg = self._config
        multiplier = 1.0

        # 1. Win rate adjustment
        win_rate = stats.win_rate
        if win_rate is not None:
            # win_rate_ratio ∈ (0, 2+)
            # = 1.0 when win_rate == baseline (neutral)
            # < 1.0 when underperforming, > 1.0 when outperforming
            win_rate_ratio = win_rate / max(cfg.baseline_win_rate, 0.01)
            # 映射到乘数：使用平方根来减弱极端值的影响
            # win_rate_ratio=0.5 → multiplier≈0.71; =1.5 → ≈1.22; =2.0 → ≈1.41
            multiplier = win_rate_ratio ** 0.5

        # 2. Streak penalty (only for losing streaks)
        if stats.current_streak < -cfg.streak_penalty_threshold:
            excess = abs(stats.current_streak) - cfg.streak_penalty_threshold
            penalty = cfg.streak_penalty_factor ** excess
            multiplier *= penalty

        # 3. Profit factor adjustment (subtle)
        pf = stats.profit_factor
        if pf is not None and stats.total >= 2:
            if pf < 0.8:
                # 低 profit factor → 额外轻微压制
                multiplier *= 0.95
            elif pf > 2.0:
                # 高 profit factor → 微幅提升
                multiplier *= 1.05
        elif pf is None and stats.losses > 0 and stats.total_win_pnl == 0 and stats.total >= 2:
            # 纯亏损情况（有 losses 但 win pnl 全为 0），PF 无法计算但仍应压制
            multiplier *= 0.90

        # Clamp to bounds
        return max(cfg.min_multiplier, min(cfg.max_multiplier, multiplier))

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def reset_session(self) -> Dict[str, Any]:
        """重置所有日内统计，返回重置前的 summary。"""
        with self._lock:
            summary = self._build_summary_unlocked()
            self._stats.clear()
            self._regime_stats.clear()
            self._category_stats.clear()
            self._total_recorded = 0
            self._session_start_at = time.monotonic()
        logger.info(
            "StrategyPerformanceTracker: session reset (had %d outcomes)",
            summary.get("total_recorded", 0),
        )
        return summary

    def check_session_reset(self) -> bool:
        """检查是否应自动重置 session（基于配置的间隔）。返回是否执行了重置。"""
        interval = self._config.session_reset_interval_hours
        if interval <= 0:
            return False
        elapsed_hours = (time.monotonic() - self._session_start_at) / 3600.0
        if elapsed_hours >= interval:
            self.reset_session()
            return True
        return False

    # ------------------------------------------------------------------
    # Query / monitoring
    # ------------------------------------------------------------------

    def get_strategy_stats(self, strategy: str) -> Optional[Dict[str, Any]]:
        """返回单个策略的日内统计快照（含 regime 维度细分）。"""
        with self._lock:
            stats = self._stats.get(strategy)
            if stats is None:
                return None
            result = stats.to_dict()
            result["multiplier"] = self.get_multiplier(strategy)
            # regime 维度细分
            regime_breakdown: Dict[str, Dict[str, Any]] = {}
            for (strat, regime_val), r_stats in self._regime_stats.items():
                if strat == strategy and r_stats.total > 0:
                    entry = r_stats.to_dict()
                    entry["multiplier"] = self._compute_multiplier(r_stats)
                    regime_breakdown[regime_val] = entry
            if regime_breakdown:
                result["regime_breakdown"] = regime_breakdown
            return result

    def describe(self) -> Dict[str, Any]:
        """返回 PerformanceTracker 的全局状态，用于监控端点。"""
        with self._lock:
            return self._build_summary_unlocked()

    def _build_summary_unlocked(self) -> Dict[str, Any]:
        """不加锁地构建 summary（调用者负责持锁）。"""
        strategy_summaries: Dict[str, Dict[str, Any]] = {}
        for name, stats in self._stats.items():
            entry = stats.to_dict()
            entry["category"] = self._strategy_categories.get(name, "unknown")
            entry["multiplier"] = self._compute_multiplier(stats)
            # regime 维度细分
            regime_breakdown: Dict[str, Dict[str, Any]] = {}
            for (strat, regime_val), r_stats in self._regime_stats.items():
                if strat == name and r_stats.total > 0:
                    r_entry = r_stats.to_dict()
                    r_entry["multiplier"] = self._compute_multiplier(r_stats)
                    regime_breakdown[regime_val] = r_entry
            if regime_breakdown:
                entry["regime_breakdown"] = regime_breakdown
            strategy_summaries[name] = entry

        category_summaries: Dict[str, Dict[str, Any]] = {}
        for cat, stats in self._category_stats.items():
            category_summaries[cat] = stats.to_dict()

        return {
            "enabled": self._config.enabled,
            "total_recorded": self._total_recorded,
            "session_duration_minutes": round(
                (time.monotonic() - self._session_start_at) / 60.0, 1
            ),
            "strategies": strategy_summaries,
            "categories": category_summaries,
            "config": {
                "baseline_win_rate": self._config.baseline_win_rate,
                "min_multiplier": self._config.min_multiplier,
                "max_multiplier": self._config.max_multiplier,
                "streak_penalty_threshold": self._config.streak_penalty_threshold,
                "category_fallback_min_samples": self._config.category_fallback_min_samples,
            },
        }

    def strategy_ranking(self) -> List[Dict[str, Any]]:
        """按乘数排序返回策略排名，用于诊断。"""
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for name, stats in self._stats.items():
                if stats.total == 0:
                    continue
                entry = stats.to_dict()
                entry["strategy"] = name
                entry["category"] = self._strategy_categories.get(name, "unknown")
                entry["multiplier"] = self._compute_multiplier(stats)
                # regime 维度细分
                regime_breakdown: Dict[str, Dict[str, Any]] = {}
                for (strat, regime_val), r_stats in self._regime_stats.items():
                    if strat == name and r_stats.total > 0:
                        r_entry = r_stats.to_dict()
                        r_entry["multiplier"] = self._compute_multiplier(r_stats)
                        regime_breakdown[regime_val] = r_entry
                if regime_breakdown:
                    entry["regime_breakdown"] = regime_breakdown
                rows.append(entry)
        rows.sort(key=lambda r: r.get("multiplier", 1.0), reverse=True)
        return rows
