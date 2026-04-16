"""Paper Trading ↔ Backtest 对账工具。

职责：
    给定一段时间窗口，拉取 paper trading 真实成交统计，并运行等价回测，
    输出"两边是否一致"的诊断报告。

判定标准（divergence tolerances 可通过 CLI 调整）：
    - trades 计数差异 > 20%       → ALARM
    - win_rate 差异 > 10 个点     → ALARM
    - total_pnl 差异（按 volume 归一化）> 25%  → ALARM
    - avg_slippage_cost 差异 > 30%  → WARN

任何 ALARM 意味着回测无法真实代表实盘——**上实盘前必须排清**。

退出码：
    0 = 基本一致
    1 = 有 WARN 或 ALARM
    2 = 基础设施错误
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── 契约 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReconciliationConfig:
    """对账参数契约。"""

    session_id: Optional[str]
    start: str
    end: str
    symbol: str
    timeframe: str
    trades_divergence_alarm: float       # relative, e.g. 0.20
    win_rate_divergence_alarm: float     # absolute percentage points, e.g. 0.10
    pnl_divergence_alarm: float          # relative, e.g. 0.25
    slippage_divergence_warn: float      # relative, e.g. 0.30


@dataclass(frozen=True)
class AggregateStats:
    source: str                 # "paper" | "backtest"
    trades: int
    win_rate: float             # [0, 1]
    total_pnl: float
    avg_slippage_cost: float
    avg_bars_held: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "trades": self.trades,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "avg_slippage_cost": round(self.avg_slippage_cost, 4),
            "avg_bars_held": round(self.avg_bars_held, 1),
        }


@dataclass(frozen=True)
class DivergenceAlert:
    metric: str
    paper_value: float
    backtest_value: float
    divergence: float      # relative for counts/pnl, absolute for win_rate
    severity: str          # "alarm" | "warn" | "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "paper_value": round(self.paper_value, 4),
            "backtest_value": round(self.backtest_value, 4),
            "divergence": round(self.divergence, 4),
            "severity": self.severity,
        }


@dataclass(frozen=True)
class ReconciliationReport:
    config: ReconciliationConfig
    paper_stats: AggregateStats
    backtest_stats: AggregateStats
    alerts: Tuple[DivergenceAlert, ...] = field(default_factory=tuple)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def max_severity(self) -> str:
        if any(a.severity == "alarm" for a in self.alerts):
            return "alarm"
        if any(a.severity == "warn" for a in self.alerts):
            return "warn"
        return "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "start": self.config.start,
            "end": self.config.end,
            "symbol": self.config.symbol,
            "timeframe": self.config.timeframe,
            "paper": self.paper_stats.to_dict(),
            "backtest": self.backtest_stats.to_dict(),
            "alerts": [a.to_dict() for a in self.alerts],
            "max_severity": self.max_severity(),
        }


# ── 数据拉取 ────────────────────────────────────────────────────


def _fetch_paper_stats(cfg: ReconciliationConfig) -> AggregateStats:
    """从 DB 拉取 paper_trade_outcomes 并聚合。"""
    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter
    from src.persistence.repositories.paper_trading_repo import PaperTradingRepository

    start_dt = datetime.fromisoformat(cfg.start).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(cfg.end).replace(tzinfo=timezone.utc)

    writer = TimescaleWriter(load_db_settings(), min_conn=1, max_conn=2)
    try:
        repo = PaperTradingRepository(writer)
        trades = repo.fetch_trades(
            session_id=cfg.session_id,
            symbol=cfg.symbol,
            limit=10000,
        )
    finally:
        writer.close()

    # 按时间窗口筛选（fetch_trades 无时间参数，在应用层过滤）
    filtered = [
        t for t in trades
        if t.get("entry_time") is not None
        and start_dt <= t["entry_time"] <= end_dt
        and t.get("timeframe") == cfg.timeframe
    ]

    return _aggregate(filtered, source="paper")


def _fetch_backtest_stats(cfg: ReconciliationConfig) -> AggregateStats:
    """运行等价回测并聚合交易。"""
    from src.backtesting.component_factory import (
        _load_signal_config_snapshot,
        build_backtest_components,
    )
    from src.backtesting.config import get_backtest_defaults
    from src.backtesting.engine import BacktestEngine
    from src.backtesting.models import BacktestConfig

    optimizer_only = {"search_mode", "max_combinations", "sort_metric"}
    defaults = {
        k: v for k, v in get_backtest_defaults().items() if k not in optimizer_only
    }
    signal_config = _load_signal_config_snapshot()
    strategy_sessions = {
        name: list(sess_list)
        for name, sess_list in signal_config.strategy_sessions.items()
        if sess_list
    }
    strategy_timeframes = {
        name: list(tf_list)
        for name, tf_list in signal_config.strategy_timeframes.items()
        if tf_list
    }
    defaults["trailing_tp_enabled"] = bool(signal_config.trailing_tp_enabled)
    defaults["trailing_tp_activation_atr"] = float(signal_config.trailing_tp_activation_atr)
    defaults["trailing_tp_trail_atr"] = float(signal_config.trailing_tp_trail_atr)

    bt_config = BacktestConfig.from_flat(
        symbol=cfg.symbol,
        timeframe=cfg.timeframe,
        start_time=datetime.fromisoformat(cfg.start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(cfg.end).replace(tzinfo=timezone.utc),
        strategy_sessions=strategy_sessions,
        strategy_timeframes=strategy_timeframes,
        **defaults,
    )
    components = build_backtest_components()
    engine = BacktestEngine(
        config=bt_config,
        data_loader=components["data_loader"],
        signal_module=components["signal_module"],
        indicator_pipeline=components["pipeline"],
        regime_detector=components["regime_detector"],
        performance_tracker=components.get("performance_tracker"),
    )
    result = engine.run()

    trades: List[Dict[str, Any]] = []
    for t in result.trades:
        trades.append({
            "pnl": float(getattr(t, "pnl", 0) or 0),
            "slippage_cost": float(getattr(t, "slippage_cost", 0) or 0),
            "bars_held": int(getattr(t, "bars_held", 0) or 0),
        })
    return _aggregate(trades, source="backtest")


def _aggregate(trades: List[Dict[str, Any]], *, source: str) -> AggregateStats:
    """统一聚合逻辑（paper / backtest 共用）。"""
    n = len(trades)
    if n == 0:
        return AggregateStats(
            source=source, trades=0, win_rate=0.0,
            total_pnl=0.0, avg_slippage_cost=0.0, avg_bars_held=0.0,
        )
    wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
    total_pnl = sum((t.get("pnl") or 0.0) for t in trades)
    total_slip = sum((t.get("slippage_cost") or 0.0) for t in trades)
    total_bars = sum((t.get("bars_held") or 0) for t in trades)
    return AggregateStats(
        source=source,
        trades=n,
        win_rate=wins / n,
        total_pnl=total_pnl,
        avg_slippage_cost=total_slip / n,
        avg_bars_held=total_bars / n,
    )


# ── 分歧判定 ────────────────────────────────────────────────────


def _compute_alerts(
    paper: AggregateStats, backtest: AggregateStats, cfg: ReconciliationConfig
) -> Tuple[DivergenceAlert, ...]:
    """按 cfg 阈值判定分歧严重度。"""
    alerts: List[DivergenceAlert] = []

    # Trades count
    alerts.append(_alert_relative(
        "trades", float(paper.trades), float(backtest.trades),
        cfg.trades_divergence_alarm, severity_good="alarm",
    ))
    # Win rate (absolute)
    alerts.append(_alert_absolute(
        "win_rate", paper.win_rate, backtest.win_rate,
        cfg.win_rate_divergence_alarm, severity_good="alarm",
    ))
    # Total PnL (normalize by max(|paper|, |backtest|, 1))
    alerts.append(_alert_relative(
        "total_pnl", paper.total_pnl, backtest.total_pnl,
        cfg.pnl_divergence_alarm, severity_good="alarm",
    ))
    # Avg slippage cost (warn)
    alerts.append(_alert_relative(
        "avg_slippage_cost", paper.avg_slippage_cost, backtest.avg_slippage_cost,
        cfg.slippage_divergence_warn, severity_good="warn",
    ))
    return tuple(alerts)


def _alert_relative(
    metric: str, a: float, b: float, threshold: float, *, severity_good: str,
) -> DivergenceAlert:
    denom = max(abs(a), abs(b), 1e-9)
    div = abs(a - b) / denom
    sev = severity_good if div > threshold else "ok"
    return DivergenceAlert(
        metric=metric, paper_value=a, backtest_value=b, divergence=div, severity=sev,
    )


def _alert_absolute(
    metric: str, a: float, b: float, threshold: float, *, severity_good: str,
) -> DivergenceAlert:
    div = abs(a - b)
    sev = severity_good if div > threshold else "ok"
    return DivergenceAlert(
        metric=metric, paper_value=a, backtest_value=b, divergence=div, severity=sev,
    )


# ── 报告格式 ────────────────────────────────────────────────────


def _format(report: ReconciliationReport) -> str:
    lines = []
    lines.append("\n" + "=" * 72)
    lines.append(f"Paper ↔ Backtest Reconciliation — {report.generated_at.isoformat()}")
    lines.append("=" * 72)
    lines.append(
        f"Symbol: {report.config.symbol}  TF: {report.config.timeframe}  "
        f"Window: {report.config.start} → {report.config.end}"
    )
    lines.append("")
    lines.append(f"{'Metric':<20} {'Paper':>14} {'Backtest':>14} {'Divergence':>14} Severity")
    lines.append("-" * 72)
    p, b = report.paper_stats, report.backtest_stats
    lines.append(f"{'trades':<20} {p.trades:>14} {b.trades:>14}")
    lines.append(f"{'win_rate':<20} {p.win_rate:>14.3%} {b.win_rate:>14.3%}")
    lines.append(f"{'total_pnl':<20} {p.total_pnl:>14.2f} {b.total_pnl:>14.2f}")
    lines.append(f"{'avg_slippage_cost':<20} {p.avg_slippage_cost:>14.4f} {b.avg_slippage_cost:>14.4f}")
    lines.append(f"{'avg_bars_held':<20} {p.avg_bars_held:>14.1f} {b.avg_bars_held:>14.1f}")
    lines.append("")
    lines.append(f"Divergence alerts (max severity: {report.max_severity()}):")
    for a in report.alerts:
        marker = {"alarm": "🔴", "warn": "🟡", "ok": "✓"}.get(a.severity, "?")
        lines.append(
            f"  {marker} [{a.severity:<5}] {a.metric:<20} "
            f"paper={a.paper_value:.4f} bt={a.backtest_value:.4f} "
            f"div={a.divergence:.4f}"
        )
    lines.append("")
    return "\n".join(lines)


# ── CLI 入口 ────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare paper trading vs backtest for a given window"
    )
    parser.add_argument("--environment", choices=["live", "demo"], required=True)
    parser.add_argument("--start", required=True, help="ISO date")
    parser.add_argument("--end", required=True, help="ISO date")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", required=True)
    parser.add_argument(
        "--session-id", default="",
        help="Paper trading session id. Empty = all sessions in window",
    )
    parser.add_argument("--trades-alarm", type=float, default=0.20)
    parser.add_argument("--winrate-alarm", type=float, default=0.10)
    parser.add_argument("--pnl-alarm", type=float, default=0.25)
    parser.add_argument("--slippage-warn", type=float, default=0.30)
    parser.add_argument(
        "--output", default="",
        help="Optional JSON output path",
    )
    args = parser.parse_args()

    from src.config.instance_context import set_current_environment
    set_current_environment(args.environment)

    cfg = ReconciliationConfig(
        session_id=args.session_id or None,
        start=args.start, end=args.end,
        symbol=args.symbol, timeframe=args.timeframe.upper(),
        trades_divergence_alarm=args.trades_alarm,
        win_rate_divergence_alarm=args.winrate_alarm,
        pnl_divergence_alarm=args.pnl_alarm,
        slippage_divergence_warn=args.slippage_warn,
    )

    paper_stats = _fetch_paper_stats(cfg)
    backtest_stats = _fetch_backtest_stats(cfg)
    alerts = _compute_alerts(paper_stats, backtest_stats, cfg)

    report = ReconciliationReport(
        config=cfg,
        paper_stats=paper_stats,
        backtest_stats=backtest_stats,
        alerts=alerts,
    )
    print(_format(report))

    if args.output:
        import os

        os.makedirs(Path(args.output).parent, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
        print(f"Report saved: {args.output}")

    severity = report.max_severity()
    return 0 if severity == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
