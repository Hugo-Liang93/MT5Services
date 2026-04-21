"""Backtest detail 读模型（Phase 1 P11）。

为 `/v1/backtest/history/{run_id}/metrics-summary` 等 detail 端点装配派生字段：
- metrics_summary：从 `backtest_runs.metrics` + `fetch_trades()` 派生 R 倍数
- equity_curve：从 `backtest_runs.equity_curve` 展开 + 计算 drawdown
- monthly_returns：按月聚合 equity_curve 的期末权益差值

数据源：BacktestRepository（共享连接池，禁止在此 new TimescaleWriter —— ADR-006）。
派生逻辑全部在此文件，Repository 只做原始字段查询。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.readmodels.freshness import build_freshness_block

logger = logging.getLogger(__name__)


# freshness 阈值：回测 run 一旦完成，数据是静态的；但 API 请求仍要标 observed_at。
# 对回测 detail 而言 created_at 就是 data_updated_at，不会"过时"（除非重跑）。
# 给宽松阈值，主要用于配合前端 capability 显示。
_STALE_AFTER_SECONDS = 3600.0 * 24.0  # 24h 之内视为 fresh
_MAX_AGE_SECONDS = 3600.0 * 24.0 * 30.0  # 30 天外标 stale


class BacktestDetailReadModel:
    """P11 Phase 1：回测 detail 读模型。

    只从 BacktestRepository 读数据，派生字段（drawdown / monthly / R 倍数）在此装配。
    每请求构造（轻量），不持有状态。
    """

    def __init__(self, *, backtest_repo: Any) -> None:
        self._repo = backtest_repo

    # ────────────────────── Public API ──────────────────────

    def build_metrics_summary(self, run_id: str) -> Optional[Dict[str, Any]]:
        """运行级总指标 + 月度收益。返回 None 表示 run_id 不存在。"""
        run = self._fetch_run_or_none(run_id)
        if run is None:
            return None

        metrics = self._ensure_dict(run.get("metrics"))
        config = self._ensure_dict(run.get("config"))
        equity_curve_raw = run.get("equity_curve") or []

        initial_balance = self._coerce_float(config.get("initial_balance"), 0.0)
        max_drawdown = self._coerce_float(metrics.get("max_drawdown"), 0.0)

        # 派生 max_drawdown_pct
        if initial_balance > 0:
            max_drawdown_pct = max_drawdown / initial_balance
        else:
            max_drawdown_pct = 0.0

        # 派生 R 倍数（需要读 trades）
        expectancy_r, avg_r_multiple = self._compute_r_metrics(run_id)

        # 月度收益
        monthly = self._compute_monthly_returns(
            equity_curve_raw,
            initial_balance=initial_balance,
            trades=self._fetch_trades_or_empty(run_id),
        )

        freshness = self._freshness(run.get("created_at"))

        return {
            "run_id": run_id,
            "total_pnl": self._coerce_float(metrics.get("total_pnl"), 0.0),
            "total_pnl_pct": self._coerce_float(metrics.get("total_pnl_pct"), 0.0),
            "total_trades": int(metrics.get("total_trades") or 0),
            "winning_trades": int(metrics.get("winning_trades") or 0),
            "losing_trades": int(metrics.get("losing_trades") or 0),
            "win_rate": self._coerce_float(metrics.get("win_rate"), 0.0),
            "profit_factor": self._coerce_float(metrics.get("profit_factor"), 0.0),
            "expectancy": self._coerce_float(metrics.get("expectancy"), 0.0),
            "expectancy_r": expectancy_r,
            "avg_r_multiple": avg_r_multiple,
            "max_drawdown": max_drawdown,
            "max_drawdown_pct": max_drawdown_pct,
            "max_drawdown_duration_bars": int(
                metrics.get("max_drawdown_duration") or 0
            ),
            "sharpe_ratio": self._coerce_float(metrics.get("sharpe_ratio"), 0.0),
            "sortino_ratio": self._coerce_float(metrics.get("sortino_ratio"), 0.0),
            "calmar_ratio": self._coerce_float(metrics.get("calmar_ratio"), 0.0),
            "avg_win": self._coerce_float(metrics.get("avg_win"), 0.0),
            "avg_loss": self._coerce_float(metrics.get("avg_loss"), 0.0),
            "avg_bars_held": self._coerce_float(metrics.get("avg_bars_held"), 0.0),
            "max_consecutive_wins": int(metrics.get("max_consecutive_wins") or 0),
            "max_consecutive_losses": int(metrics.get("max_consecutive_losses") or 0),
            "monthly_returns": monthly,
            "freshness": freshness,
        }

    def build_equity_curve(self, run_id: str) -> Optional[Dict[str, Any]]:
        """净值曲线（含派生 drawdown / drawdown_pct / pnl_cumulative）。"""
        run = self._fetch_run_or_none(run_id)
        if run is None:
            return None

        config = self._ensure_dict(run.get("config"))
        initial_balance = self._coerce_float(config.get("initial_balance"), 0.0)
        raw_points = run.get("equity_curve") or []

        points = self._expand_equity_curve(raw_points, initial_balance=initial_balance)

        return {
            "run_id": run_id,
            "initial_balance": initial_balance,
            "points": points,
            "freshness": self._freshness(run.get("created_at")),
        }

    def build_monthly_returns(self, run_id: str) -> Optional[Dict[str, Any]]:
        """月度收益。"""
        run = self._fetch_run_or_none(run_id)
        if run is None:
            return None

        config = self._ensure_dict(run.get("config"))
        initial_balance = self._coerce_float(config.get("initial_balance"), 0.0)
        raw_points = run.get("equity_curve") or []

        months = self._compute_monthly_returns(
            raw_points,
            initial_balance=initial_balance,
            trades=self._fetch_trades_or_empty(run_id),
        )

        return {
            "run_id": run_id,
            "initial_balance": initial_balance,
            "months": months,
            "freshness": self._freshness(run.get("created_at")),
        }

    # ────────────────────── Phase 4a: Trade Structure ──────────────────────

    def build_trade_structure(self, run_id: str) -> Optional[Dict[str, Any]]:
        """派生交易结构指标：avg_hold / max_loss_streak / mfe-mae 分布 / holding buckets。

        数据缺失处理：trades 为空 → total_trades=0；部分 trades 缺 mfe/mae/hold
        → partial_data=True，avg_* 只统计有数据的部分。
        """
        run = self._fetch_run_or_none(run_id)
        if run is None:
            return None

        trades = self._fetch_trades_or_empty(run_id)
        total_trades = len(trades)

        # hold_minutes
        hold_values = [
            t["hold_minutes"] for t in trades if t.get("hold_minutes") is not None
        ]
        avg_hold = sum(hold_values) / len(hold_values) if hold_values else None
        median_hold = _median(hold_values) if hold_values else None

        # MFE / MAE
        mfe_values = [t["mfe_pct"] for t in trades if t.get("mfe_pct") is not None]
        mae_values = [t["mae_pct"] for t in trades if t.get("mae_pct") is not None]
        avg_mfe = sum(mfe_values) / len(mfe_values) if mfe_values else None
        avg_mae = sum(mae_values) / len(mae_values) if mae_values else None

        # 连亏 / 连胜（从 pnl 符号推导，不依赖 mfe/mae）
        max_win_streak, max_loss_streak = _compute_streaks(
            [self._coerce_float(t.get("pnl"), 0.0) for t in trades]
        )

        mfe_distribution = _distribution_buckets(mfe_values, is_positive=True)
        mae_distribution = _distribution_buckets(mae_values, is_positive=True)
        holding_buckets = _holding_buckets(trades)

        partial_data = total_trades > 0 and (
            len(hold_values) < total_trades
            or len(mfe_values) < total_trades
            or len(mae_values) < total_trades
        )

        return {
            "run_id": run_id,
            "total_trades": total_trades,
            "partial_data": partial_data,
            "avg_hold_minutes": (round(avg_hold, 2) if avg_hold is not None else None),
            "median_hold_minutes": (
                round(median_hold, 2) if median_hold is not None else None
            ),
            "max_loss_streak": max_loss_streak,
            "max_win_streak": max_win_streak,
            "avg_mfe_pct": (round(avg_mfe, 4) if avg_mfe is not None else None),
            "avg_mae_pct": (round(avg_mae, 4) if avg_mae is not None else None),
            "mfe_distribution": mfe_distribution,
            "mae_distribution": mae_distribution,
            "holding_buckets": holding_buckets,
            "freshness": self._freshness(run.get("created_at")),
        }

    # ────────────────────── Phase 4a: Execution Realism ──────────────────────

    def build_execution_realism(self, run_id: str) -> Optional[Dict[str, Any]]:
        """读取运行级执行现实性字段。

        Phase 4a 占位：如果 `backtest_runs.execution_realism` 已落库则透出；否则
        所有敏感性字段返回 None，前端按 null 渲染。
        """
        run = self._fetch_run_or_none(run_id)
        if run is None:
            return None

        realism_raw = self._ensure_dict(run.get("execution_realism"))
        available = bool(realism_raw)

        return {
            "run_id": run_id,
            "available": available,
            "spread_sensitivity": realism_raw.get("spread_sensitivity"),
            "slippage_sensitivity": realism_raw.get("slippage_sensitivity"),
            "broker_variance": realism_raw.get("broker_variance"),
            "session_dependence": realism_raw.get("session_dependence"),
            "freshness": self._freshness(run.get("created_at")),
        }

    # ────────────────────── Repo access helpers ──────────────────────

    def _fetch_run_or_none(self, run_id: str) -> Optional[Dict[str, Any]]:
        if self._repo is None:
            logger.warning("backtest_repo unavailable, cannot fetch run %s", run_id)
            return None
        run = self._repo.fetch_run(run_id)
        return run if isinstance(run, dict) else None

    def _fetch_trades_or_empty(self, run_id: str) -> List[Dict[str, Any]]:
        if self._repo is None:
            return []
        try:
            trades = self._repo.fetch_trades(run_id)
        except Exception:
            logger.debug("fetch_trades failed for %s", run_id, exc_info=True)
            return []
        return list(trades) if trades else []

    # ────────────────────── Derivation ──────────────────────

    @staticmethod
    def _expand_equity_curve(
        raw: Iterable[Any],
        *,
        initial_balance: float,
    ) -> List[Dict[str, Any]]:
        """展开 equity_curve 并派生 drawdown / drawdown_pct / pnl_cumulative。

        raw 每项为 [iso_str, equity_value]（Repo 存成 JSON 数组）；容错 tuple / list。
        """
        points: List[Dict[str, Any]] = []
        running_max: Optional[float] = None

        for item in raw:
            ts, equity = BacktestDetailReadModel._unpack_curve_item(item)
            if ts is None or equity is None:
                continue

            if running_max is None or equity > running_max:
                running_max = equity

            drawdown = max(
                0.0, (running_max - equity) if running_max is not None else 0.0
            )
            drawdown_pct = (
                drawdown / running_max if running_max and running_max > 0 else 0.0
            )
            pnl_cumulative = equity - initial_balance

            points.append(
                {
                    "label": ts,
                    "timestamp": ts,
                    "equity": float(equity),
                    "drawdown": float(drawdown),
                    "drawdown_pct": float(drawdown_pct),
                    "pnl_cumulative": float(pnl_cumulative),
                }
            )
        return points

    @staticmethod
    def _unpack_curve_item(item: Any) -> Tuple[Optional[str], Optional[float]]:
        """兼容 JSONB 存储的 [ts, value] 或 (ts, value)。"""
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            ts_raw, value_raw = item[0], item[1]
        elif isinstance(item, dict):
            ts_raw = item.get("timestamp") or item.get("time") or item.get("label")
            value_raw = item.get("equity") or item.get("balance") or item.get("value")
        else:
            return None, None

        ts = str(ts_raw) if ts_raw is not None else None
        try:
            value = float(value_raw) if value_raw is not None else None
        except (TypeError, ValueError):
            value = None
        return ts, value

    def _compute_monthly_returns(
        self,
        raw_equity_curve: Iterable[Any],
        *,
        initial_balance: float,
        trades: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """按月聚合：

        - pnl = 月末 equity - 月初 equity（取每月第一个数据点作为月初，最后一个作为月末）
        - return_pct = pnl / 月初 equity（或 initial_balance 当月初 = 0）
        - trade_count = 按 exit_time 归属到该月的 trades 数
        """
        monthly_points: "OrderedDict[str, Dict[str, float]]" = OrderedDict()

        for item in raw_equity_curve:
            ts, equity = self._unpack_curve_item(item)
            if ts is None or equity is None:
                continue
            month_key = self._month_key_from_iso(ts)
            if month_key is None:
                continue
            bucket = monthly_points.setdefault(
                month_key,
                {"first_equity": equity, "last_equity": equity},
            )
            bucket["last_equity"] = equity
            # first_equity 初次赋值后不再覆盖（保留月初值）

        # 按月统计交易数
        trade_counts: Dict[str, int] = {}
        for trade in trades:
            exit_time = trade.get("exit_time")
            if not isinstance(exit_time, str):
                continue
            month_key = self._month_key_from_iso(exit_time)
            if month_key is None:
                continue
            trade_counts[month_key] = trade_counts.get(month_key, 0) + 1

        results: List[Dict[str, Any]] = []
        previous_month_equity: Optional[float] = None
        for month_key, bucket in monthly_points.items():
            # 以"上月末" 或 "首月 initial_balance" 作为本月 starting equity
            starting_equity = (
                previous_month_equity
                if previous_month_equity is not None
                else (
                    bucket["first_equity"] if initial_balance <= 0 else initial_balance
                )
            )
            if previous_month_equity is None and initial_balance > 0:
                # 首月：pnl = first_equity - initial_balance + (last - first)
                pnl = bucket["last_equity"] - initial_balance
            else:
                pnl = bucket["last_equity"] - starting_equity

            if starting_equity and starting_equity != 0:
                return_pct = pnl / starting_equity
            else:
                return_pct = 0.0

            results.append(
                {
                    "label": month_key,
                    "pnl": float(pnl),
                    "return_pct": float(return_pct),
                    "trade_count": int(trade_counts.get(month_key, 0)),
                }
            )
            previous_month_equity = bucket["last_equity"]

        return results

    def _compute_r_metrics(
        self, run_id: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """从 trades 派生 expectancy_r 和 avg_r_multiple。

        R 倍数 = pnl / initial_risk，其中 initial_risk = |entry - stop_loss| × position_size × contract_size。
        contract_size 未在 fetch_trades 返回（不在 backtest_trades 表列），因此退而用：
        initial_risk_proxy = |entry - stop_loss| × position_size（价格点 × 手数）。
        对前端 R 倍数排序和决策建议这是一致的 proxy（量纲归一）。

        trades 为空或全部 stop_loss 无效（= entry_price）时返回 (None, None)。
        """
        trades = self._fetch_trades_or_empty(run_id)
        if not trades:
            return None, None

        r_multiples: List[float] = []
        for trade in trades:
            entry = self._coerce_float(trade.get("entry_price"), 0.0)
            stop_loss = self._coerce_float(trade.get("stop_loss"), 0.0)
            position_size = self._coerce_float(trade.get("position_size"), 0.0)
            pnl = self._coerce_float(trade.get("pnl"), 0.0)

            risk_distance = abs(entry - stop_loss)
            if risk_distance <= 0 or position_size <= 0:
                continue
            initial_risk = risk_distance * position_size
            if initial_risk <= 0:
                continue
            r_multiples.append(pnl / initial_risk)

        if not r_multiples:
            return None, None

        mean_r = sum(r_multiples) / len(r_multiples)
        # expectancy_r 与 avg_r_multiple 同源（per-trade R 的均值即为 R 单位期望）
        return mean_r, mean_r

    # ────────────────────── Helpers ──────────────────────

    @staticmethod
    def _ensure_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _coerce_float(value: Any, default: float) -> float:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _month_key_from_iso(iso_str: str) -> Optional[str]:
        """'2026-04-15T10:00:00+00:00' → '2026-04'。"""
        try:
            # 快速路径：直接取前 7 字符（ISO8601 保证前 7 位是 YYYY-MM）
            if len(iso_str) >= 7 and iso_str[4] == "-":
                return iso_str[:7]
            dt = datetime.fromisoformat(iso_str)
            return f"{dt.year:04d}-{dt.month:02d}"
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _freshness(created_at: Any) -> Dict[str, Any]:
        observed_at = datetime.now(timezone.utc).isoformat()
        return build_freshness_block(
            observed_at=observed_at,
            data_updated_at=created_at,
            stale_after_seconds=_STALE_AFTER_SECONDS,
            max_age_seconds=_MAX_AGE_SECONDS,
            source_kind="native",
        )


# ────────────────────── Phase 4a Helpers ──────────────────────


def _median(values: List[float]) -> float:
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 1:
        return float(sorted_values[mid])
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2.0


def _compute_streaks(pnls: List[float]) -> Tuple[int, int]:
    """从 pnl 符号序列算 (max_win_streak, max_loss_streak)。"""
    max_win = max_loss = 0
    cur_win = cur_loss = 0
    for p in pnls:
        if p > 0:
            cur_win += 1
            cur_loss = 0
            max_win = max(max_win, cur_win)
        elif p < 0:
            cur_loss += 1
            cur_win = 0
            max_loss = max(max_loss, cur_loss)
        else:
            cur_win = cur_loss = 0
    return max_win, max_loss


def _distribution_buckets(
    values: List[float], *, is_positive: bool = True
) -> List[Dict[str, Any]]:
    """MFE/MAE 分布直方图（固定 5 个桶）。

    桶边界（百分比）：[0, 0.5), [0.5, 1.0), [1.0, 2.0), [2.0, 5.0), [5.0, +inf)
    适合 XAUUSD 等标的的典型波动量级；未来可按 symbol 调整。
    """
    if not values:
        return []
    edges = [0.0, 0.5, 1.0, 2.0, 5.0]  # 上界由下一个 edge 给出；最后一个桶 +inf
    buckets: List[Dict[str, Any]] = []
    for i, lower in enumerate(edges):
        upper: Optional[float] = edges[i + 1] if i + 1 < len(edges) else None
        count = sum(1 for v in values if v >= lower and (upper is None or v < upper))
        buckets.append(
            {
                "lower_pct": float(lower),
                "upper_pct": float(upper) if upper is not None else None,
                "count": int(count),
            }
        )
    return buckets


def _holding_buckets(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按持仓时长分桶（<1h / 1-4h / 4-24h / >1d）并聚合 avg_pnl_pct。"""
    if not trades:
        return []
    edges = [
        ("<1h", 0, 60),
        ("1-4h", 60, 240),
        ("4-24h", 240, 1440),
        (">1d", 1440, None),
    ]
    buckets: List[Dict[str, Any]] = []
    for label, lower, upper in edges:
        in_bucket = [
            t
            for t in trades
            if (t.get("hold_minutes") is not None)
            and t["hold_minutes"] >= lower
            and (upper is None or t["hold_minutes"] < upper)
        ]
        count = len(in_bucket)
        avg_pnl_pct: Optional[float] = None
        if in_bucket:
            pnl_pcts = [
                float(t.get("pnl_pct") or 0.0)
                for t in in_bucket
                if t.get("pnl_pct") is not None
            ]
            if pnl_pcts:
                avg_pnl_pct = round(sum(pnl_pcts) / len(pnl_pcts), 4)
        buckets.append(
            {
                "label": label,
                "lower_minutes": int(lower),
                "upper_minutes": int(upper) if upper is not None else None,
                "trade_count": count,
                "avg_pnl_pct": avg_pnl_pct,
            }
        )
    return buckets
