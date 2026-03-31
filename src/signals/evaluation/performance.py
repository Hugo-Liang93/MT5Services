"""日内策略绩效追踪器（StrategyPerformanceTracker）。

## 核心问题

ConfidenceCalibrator 需要 DB + 50 个历史样本才能生效，
对当天日内交易的反馈太慢。策略可能在当天连续亏损，
但 calibrator 仍然基于 7 天历史数据给出 boost。

## 解决方案

StrategyPerformanceTracker 是纯内存的日内实时反馈层：
  - 接收 SignalQualityTracker / TradeOutcomeTracker 的结果回调
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
        # Clamp to prevent extreme values when loss is very small
        raw = self.total_win_pnl / abs(self.total_loss_pnl)
        return min(raw, 50.0)

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
    pnl_circuit_enabled:
        是否启用基于真实亏损的熔断器（区别于 TradeExecutor 的技术失败熔断）。
    pnl_circuit_max_consecutive_losses:
        连续实际亏损次数达到此值后暂停自动交易。
    pnl_circuit_cooldown_minutes:
        PnL 熔断开路后自动恢复的等待时间（分钟），0 = 不自动恢复。
    """

    enabled: bool = True
    baseline_win_rate: float = 0.50
    min_multiplier: float = 0.80
    max_multiplier: float = 1.20
    streak_penalty_threshold: int = 3
    streak_penalty_factor: float = 0.90
    category_fallback_min_samples: int = 3
    # 冷启动保护：样本不足此值时 multiplier 固定为 1.0（不压制也不提升）
    min_samples_for_penalty: int = 3
    session_reset_interval_hours: int = 8
    # PnL 熔断器（计实际亏损次数，不计技术故障）
    pnl_circuit_enabled: bool = True
    pnl_circuit_max_consecutive_losses: int = 5
    pnl_circuit_cooldown_minutes: int = 120


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
        # per-strategy trade-only stats（仅来自 source="trade" 的实际交易数据）
        self._trade_stats: Dict[str, _StrategyStats] = {}
        self._session_start_at: float = time.monotonic()
        self._total_recorded: int = 0
        # PnL 熔断器状态（账户级别，仅追踪 source="trade" 的连续亏损）
        self._global_trade_loss_streak: int = 0
        self._pnl_circuit_paused: bool = False
        self._pnl_circuit_opened_at: float = 0.0  # monotonic

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
        source: str = "signal",
    ) -> None:
        """记录一次策略评估结果。

        由 SignalQualityTracker（source="signal"）或
        TradeOutcomeTracker（source="trade"）回调。

        参数
        ----
        strategy: 策略名称
        won: 是否盈利
        pnl: 盈亏金额（可选，用于 profit_factor 计算）
        action: buy/sell（预留，当前未使用）
        regime: 信号发出时的市场 regime（用于 per-regime 维度统计）
        source: 数据来源 "signal"（信号质量）| "trade"（实际交易）
        """
        if not self._config.enabled:
            return

        with self._lock:
            # 按策略记录（合并所有 source）
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

            # trade source 单独记录（实际交易数据优先级更高）
            if source == "trade":
                trade_stats = self._trade_stats.setdefault(strategy, _StrategyStats())
                trade_stats.record(won, pnl)
                # PnL 熔断器：追踪账户级别的连续实际亏损
                if self._config.pnl_circuit_enabled:
                    if won:
                        self._global_trade_loss_streak = 0
                    else:
                        self._global_trade_loss_streak += 1
                        if (
                            not self._pnl_circuit_paused
                            and self._global_trade_loss_streak
                            >= self._config.pnl_circuit_max_consecutive_losses
                        ):
                            self._pnl_circuit_paused = True
                            self._pnl_circuit_opened_at = time.monotonic()
                            logger.warning(
                                "PnL circuit breaker OPEN: %d consecutive losses, "
                                "pausing %d min",
                                self._global_trade_loss_streak,
                                self._config.pnl_circuit_cooldown_minutes,
                            )

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

            # 当有实际交易数据（trade source）时优先使用，因为含真实滑点和成交价
            primary_stats = self._stats.get(strategy)
            trade_stats = self._trade_stats.get(strategy)
            if trade_stats is not None and trade_stats.total >= self._config.category_fallback_min_samples:
                # trade 数据充足：trade 0.7 + signal 0.3
                trade_mult = self._compute_multiplier(trade_stats)
                if primary_stats is not None and primary_stats.total > 0:
                    signal_mult = self._compute_multiplier(primary_stats)
                    return trade_mult * 0.7 + signal_mult * 0.3
                return trade_mult

            # 回退到全局策略统计（signal+trade 合并）
            stats = primary_stats
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
        0. 冷启动保护 → 样本不足时返回 1.0
        1. win_rate vs baseline → 基础乘数
        2. 连败惩罚 → 额外折扣
        3. profit_factor → 微调
        """
        cfg = self._config
        multiplier = 1.0

        # 0. 冷启动保护：样本不足时不压制
        if stats.total < cfg.min_samples_for_penalty:
            return 1.0

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

        # 3. Profit factor adjustment (graduated)
        pf = stats.profit_factor
        if pf is not None and stats.total >= 2:
            if pf < 0.5:
                multiplier *= 0.90  # 极差：亏损远超盈利
            elif pf < 0.8:
                multiplier *= 0.95  # 偏差
            elif pf < 1.0:
                multiplier *= 0.98  # 略亏：仍应轻微压制
            elif pf > 2.0:
                multiplier *= 1.05  # 优秀
            elif pf > 1.5:
                multiplier *= 1.02  # 良好
        elif pf is None and stats.total >= 2:
            if stats.losses > 0 and stats.total_win_pnl == 0:
                multiplier *= 0.90  # 纯亏损
            elif stats.wins > 0 and stats.total_loss_pnl == 0:
                multiplier *= 1.05  # 纯利

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
            self._trade_stats.clear()
            self._total_recorded = 0
            self._session_start_at = time.monotonic()
            # PnL 熔断器随 session 重置一起清零
            self._global_trade_loss_streak = 0
            self._pnl_circuit_paused = False
            self._pnl_circuit_opened_at = 0.0
        logger.info(
            "StrategyPerformanceTracker: session reset (had %d outcomes)",
            summary.get("total_recorded", 0),
        )
        return summary

    def is_trading_paused(self) -> bool:
        """检查 PnL 熔断器是否处于开路状态（暂停自动交易）。

        若 cooldown 超时则自动复位。线程安全。
        """
        with self._lock:
            if not self._pnl_circuit_paused:
                return False
            cooldown = self._config.pnl_circuit_cooldown_minutes
            if cooldown > 0:
                elapsed = (time.monotonic() - self._pnl_circuit_opened_at) / 60.0
                if elapsed >= cooldown:
                    self._pnl_circuit_paused = False
                    self._global_trade_loss_streak = 0
                    logger.info(
                        "PnL circuit breaker RESET after %.1f min cooldown", elapsed
                    )
                    return False
            return self._pnl_circuit_paused

    def warm_up_from_db(self, rows: List[Dict[str, Any]]) -> int:
        """从 DB 历史记录重放 outcomes，恢复重启前的 wins/losses/streak 状态。

        注意：warm_up 期间暂时禁用 PnL 熔断器触发。
        原因：warm_up 加载的是历史数据（如过夜仓强平），不应让历史亏损
        触发本次 session 的交易暂停。重放完成后重置 loss streak。
        """
        count = 0
        # 暂存 PnL circuit 状态，warm_up 期间不触发
        saved_pnl_enabled = self._config.pnl_circuit_enabled
        self._config.pnl_circuit_enabled = False
        try:
            for row in rows:
                strategy = row.get("strategy")
                won = row.get("won")
                if not strategy or won is None:
                    continue
                try:
                    pnl = float(row.get("pnl") or 0.0)
                except (TypeError, ValueError):
                    logger.warning(
                        "warm_up_from_db: invalid pnl=%r for strategy=%s, defaulting to 0.0",
                        row.get("pnl"), strategy,
                    )
                    pnl = 0.0
                regime = row.get("regime")
                source = str(row.get("source") or "signal")
                self.record_outcome(strategy, bool(won), pnl, regime=regime, source=source)
                count += 1
        finally:
            self._config.pnl_circuit_enabled = saved_pnl_enabled
            # 重置 loss streak：历史连败不应影响新 session
            self._global_trade_loss_streak = 0
            self._pnl_circuit_paused = False
            self._pnl_circuit_opened_at = 0.0
        if count:
            logger.info(
                "StrategyPerformanceTracker: restored %d outcomes from DB "
                "(pnl_circuit reset after warm_up)",
                count,
            )
        return count

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
        regime_by_strategy = self._regime_reverse_index()

        strategy_summaries: Dict[str, Dict[str, Any]] = {}
        for name, stats in self._stats.items():
            entry = stats.to_dict()
            entry["category"] = self._strategy_categories.get(name, "unknown")
            entry["multiplier"] = self._compute_multiplier(stats)
            per_regime = regime_by_strategy.get(name)
            if per_regime:
                regime_breakdown: Dict[str, Dict[str, Any]] = {}
                for regime_val, r_stats in per_regime.items():
                    r_entry = r_stats.to_dict()
                    r_entry["multiplier"] = self._compute_multiplier(r_stats)
                    regime_breakdown[regime_val] = r_entry
                entry["regime_breakdown"] = regime_breakdown
            # 附加 trade source 的独立统计（如有）
            t_stats = self._trade_stats.get(name)
            if t_stats is not None and t_stats.total > 0:
                entry["trade_stats"] = t_stats.to_dict()
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

    def _regime_reverse_index(self) -> Dict[str, Dict[str, "_StrategyStats"]]:
        """Build strategy → {regime: stats} reverse index (caller holds lock)."""
        idx: Dict[str, Dict[str, "_StrategyStats"]] = {}
        for (strat, regime_val), r_stats in self._regime_stats.items():
            if r_stats.total > 0:
                idx.setdefault(strat, {})[regime_val] = r_stats
        return idx

    def strategy_ranking(self) -> List[Dict[str, Any]]:
        """按乘数排序返回策略排名，用于诊断。"""
        with self._lock:
            regime_idx = self._regime_reverse_index()
            rows: List[Dict[str, Any]] = []
            for name, stats in self._stats.items():
                if stats.total == 0:
                    continue
                entry = stats.to_dict()
                entry["strategy"] = name
                entry["category"] = self._strategy_categories.get(name, "unknown")
                entry["multiplier"] = self._compute_multiplier(stats)
                per_regime = regime_idx.get(name)
                if per_regime:
                    regime_breakdown: Dict[str, Dict[str, Any]] = {}
                    for regime_val, r_stats in per_regime.items():
                        r_entry = r_stats.to_dict()
                        r_entry["multiplier"] = self._compute_multiplier(r_stats)
                        regime_breakdown[regime_val] = r_entry
                    entry["regime_breakdown"] = regime_breakdown
                rows.append(entry)
        rows.sort(key=lambda r: r.get("multiplier", 1.0), reverse=True)
        return rows
