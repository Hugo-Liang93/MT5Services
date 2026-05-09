"""Demo vs Backtest 跨库对账工具（ADR-010 替代 paper_vs_backtest）。

职责：
    给定一段 demo 时间窗口，跨库对账：
    - db.live.backtest_runs[<run_id>].metrics_by_strategy → 回测预期 metrics
    - db.demo.signal_events                                → 信号生成密度 + 风控通过率
    - db.demo.trade_outcomes                               → 实际成交统计

判定标准（divergence tolerances 可通过 CLI 调整）：
    - signal_count_drift > 30%      → ALARM（信号生成密度严重不符）
    - actionability_rate < 0.5      → WARN（风控通过率过低）
    - trades_drift > 30%            → ALARM（实际成交数量严重偏离回测）
    - win_rate_drift > 15%          → ALARM（实际胜率偏离回测）

任何 ALARM 意味着 demo 表现无法支持升级到 ACTIVE_GUARDED——**改为 ACTIVE_GUARDED 前必须排清**。

退出码：
    0 = 一致（候选可升级 ACTIVE_GUARDED）
    1 = 有 WARN/ALARM
    2 = 基础设施错误（DB 连接失败 / backtest_run_id 不存在等）

PnL 度量说明（重要）：
    demo `trade_outcomes.price_change` 是 close_price - fill_price 的 raw
    价差（e.g. XAUUSD ~$245），**未乘 lot × contract size**——schema 不
    存储这两个字段。backtest 的 `total_pnl` 是 USD（已乘 lot/contract）。
    二者单位不一致，**绝对值不可比**。本工具用 trade_count + win_rate
    drift 做 unit-free 对账；demo 侧 PnL 字段命名为 `signed_price_change`
    并仅用于方向校验（盈亏总价差 > 0 即说明 demo 总体盈利方向正确）。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── 容差契约 ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReconciliationConfig:
    """对账参数契约。"""

    backtest_run_id: str
    demo_window_start: datetime
    demo_window_end: datetime
    strategies: Tuple[str, ...]
    signal_count_drift_alarm: float = 0.30
    actionability_rate_warn: float = 0.50
    trades_drift_alarm: float = 0.30
    win_rate_drift_alarm: float = 0.15


@dataclass
class StrategyAggregate:
    """单策略跨库聚合统计。

    字段单位：
        backtest_total_pnl          — USD（已乘 lot × contract）
        demo_signed_price_change    — 价差（带方向号），未乘 lot/contract
                                       仅用作方向校验，不与 backtest USD 比。
    """

    strategy: str
    backtest_trades: int = 0
    backtest_win_rate: float = 0.0
    backtest_profit_factor: float = 0.0
    backtest_total_pnl: float = 0.0
    demo_signal_count: int = 0
    demo_actionable_count: int = 0
    demo_trades: int = 0
    demo_win_rate: float = 0.0
    demo_signed_price_change: float = 0.0

    @property
    def actionability_rate(self) -> float:
        if self.demo_signal_count <= 0:
            return 0.0
        return self.demo_actionable_count / self.demo_signal_count

    @property
    def trades_drift(self) -> float:
        """demo trades / backtest trades - 1（绝对值）。"""
        if self.backtest_trades <= 0:
            return float("inf") if self.demo_trades > 0 else 0.0
        return abs(self.demo_trades / self.backtest_trades - 1)

    @property
    def win_rate_drift(self) -> float:
        """abs(demo_win_rate - backtest_win_rate)，单位均为 0~1 比率（unit-free）。"""
        if self.demo_trades <= 0:
            return 0.0  # 没成交时不报 drift（可能是窗口太短）
        return abs(self.demo_win_rate - self.backtest_win_rate)


@dataclass
class ReconciliationReport:
    """对账输出。"""

    config_summary: Dict[str, Any]
    aggregates: List[Dict[str, Any]]
    alarms: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": self.config_summary,
            "aggregates": self.aggregates,
            "alarms": self.alarms,
            "warnings": self.warnings,
        }

    @property
    def exit_code(self) -> int:
        if self.alarms:
            return 1
        if self.warnings:
            return 1
        return 0


# ── 数据加载 ───────────────────────────────────────────────────────


def _load_backtest_metrics(
    backtest_run_id: str,
    strategies: Tuple[str, ...],
) -> Dict[str, StrategyAggregate]:
    """从 db.live.backtest_runs[run_id].metrics_by_strategy（JSONB）加载每策略
    backtest 预期。

    metrics_by_strategy 由 BacktestEngine 写入（参 backtest_repo.py:92-94），是
    `{strategy_name: BacktestMetrics_dict}`，含 total_trades / win_rate /
    profit_factor / total_pnl 等晋级判定所需字段。
    """
    from src.config import set_current_environment
    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter

    set_current_environment("live")
    settings = load_db_settings("live")
    writer = TimescaleWriter(settings)

    run = writer.backtest_repo.fetch_run(backtest_run_id)
    if run is None:
        raise ValueError(
            f"backtest_run_id={backtest_run_id} not found in db.live.backtest_runs"
        )

    aggregates: Dict[str, StrategyAggregate] = {
        s: StrategyAggregate(strategy=s) for s in strategies
    }

    metrics_by_strategy = run.get("metrics_by_strategy") or {}
    for strategy, metrics in metrics_by_strategy.items():
        if strategy not in aggregates:
            continue
        agg = aggregates[strategy]
        agg.backtest_trades = int(metrics.get("total_trades", 0) or 0)
        agg.backtest_win_rate = float(metrics.get("win_rate", 0.0) or 0.0)
        agg.backtest_profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
        agg.backtest_total_pnl = float(metrics.get("total_pnl", 0.0) or 0.0)

    return aggregates


# 抽常量便于 sentinel 测 + 审计 SQL 字段是否对齐 schema。
# signal_events 真实字段：generated_at / direction / actionability（参
# persistence/schema/signals.py 21+ MIGRATION_SQL）。
_SIGNAL_EVENTS_SQL_TEMPLATE = """
    SELECT strategy,
           COUNT(*) AS total,
           COUNT(*) FILTER (WHERE actionability = 'actionable') AS actionable
    FROM signal_events
    WHERE strategy IN ({placeholder})
      AND generated_at BETWEEN %s AND %s
      AND direction IN ('buy', 'sell')
    GROUP BY strategy
"""

# trade_outcomes 真实字段：recorded_at(close 时间, PK) / won (bool) /
# price_change (close-fill 价差，**无 lot/contract**)
# 参 persistence/schema/trade_outcomes.py + tracking/trade_outcome.py:222
# (price_change = close_price_f - trade.fill_price)。
# direction 是字符串 'buy'/'sell'；signed_change = price_change × direction_sign
# 让 buy/sell 都是"正=盈利"，与 won 语义对齐。
_TRADE_OUTCOMES_SQL_TEMPLATE = """
    SELECT strategy,
           COUNT(*) AS trades,
           COALESCE(SUM(CASE WHEN won THEN 1 ELSE 0 END), 0) AS wins,
           COALESCE(
             SUM(price_change * CASE direction WHEN 'buy' THEN 1 ELSE -1 END),
             0.0
           ) AS signed_price_change
    FROM trade_outcomes
    WHERE strategy IN ({placeholder})
      AND recorded_at BETWEEN %s AND %s
      AND won IS NOT NULL
    GROUP BY strategy
"""


def _load_demo_aggregates(
    aggregates: Dict[str, StrategyAggregate],
    window_start: datetime,
    window_end: datetime,
) -> None:
    """从 db.demo.signal_events + trade_outcomes 填充每策略 demo 实际值。"""
    from src.config import set_current_environment
    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter

    set_current_environment("demo")
    settings = load_db_settings("demo")
    writer = TimescaleWriter(settings)

    strategies = tuple(aggregates.keys())
    if not strategies:
        return

    placeholder = ",".join(["%s"] * len(strategies))

    signal_sql = _SIGNAL_EVENTS_SQL_TEMPLATE.format(placeholder=placeholder)
    with writer.connection() as conn, conn.cursor() as cur:
        cur.execute(signal_sql, (*strategies, window_start, window_end))
        for row in cur.fetchall():
            strategy, total, actionable = row[0], int(row[1] or 0), int(row[2] or 0)
            if strategy in aggregates:
                aggregates[strategy].demo_signal_count = total
                aggregates[strategy].demo_actionable_count = actionable

    trade_sql = _TRADE_OUTCOMES_SQL_TEMPLATE.format(placeholder=placeholder)
    with writer.connection() as conn, conn.cursor() as cur:
        cur.execute(trade_sql, (*strategies, window_start, window_end))
        for row in cur.fetchall():
            strategy = row[0]
            trades = int(row[1] or 0)
            wins = int(row[2] or 0)
            signed_change = float(row[3] or 0.0)
            if strategy in aggregates:
                agg = aggregates[strategy]
                agg.demo_trades = trades
                agg.demo_win_rate = wins / trades if trades > 0 else 0.0
                agg.demo_signed_price_change = signed_change


# ── 评估 ───────────────────────────────────────────────────────


def _build_report(
    config: ReconciliationConfig,
    aggregates: Dict[str, StrategyAggregate],
) -> ReconciliationReport:
    aggregates_payload: List[Dict[str, Any]] = []
    alarms: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    for strategy, agg in sorted(aggregates.items()):
        # 信号密度漂移：demo signal_count 与 backtest trades 之比反映信号生成密度
        signal_drift = 0.0
        if agg.backtest_trades > 0 and agg.demo_signal_count >= 0:
            signal_drift = abs(agg.demo_signal_count / max(agg.backtest_trades, 1) - 1)

        payload = {
            "strategy": strategy,
            "backtest": {
                "trades": agg.backtest_trades,
                "win_rate": round(agg.backtest_win_rate, 4),
                "profit_factor": round(agg.backtest_profit_factor, 4),
                "total_pnl_usd": round(agg.backtest_total_pnl, 2),
            },
            "demo": {
                "signal_count": agg.demo_signal_count,
                "actionable_count": agg.demo_actionable_count,
                "actionability_rate": round(agg.actionability_rate, 4),
                "trades": agg.demo_trades,
                "win_rate": round(agg.demo_win_rate, 4),
                # 命名明示：raw 价差，不含 lot/contract，与 backtest USD 不可比
                "signed_price_change": round(agg.demo_signed_price_change, 4),
            },
            "drifts": {
                "signal_count_drift": round(signal_drift, 4),
                "trades_drift": round(agg.trades_drift, 4),
                "win_rate_drift": round(agg.win_rate_drift, 4),
            },
        }
        aggregates_payload.append(payload)

        # 容差校验
        if signal_drift > config.signal_count_drift_alarm:
            alarms.append(
                {
                    "strategy": strategy,
                    "metric": "signal_count_drift",
                    "value": round(signal_drift, 4),
                    "threshold": config.signal_count_drift_alarm,
                }
            )
        if (
            agg.demo_signal_count > 0
            and agg.actionability_rate < config.actionability_rate_warn
        ):
            warnings.append(
                {
                    "strategy": strategy,
                    "metric": "actionability_rate",
                    "value": round(agg.actionability_rate, 4),
                    "threshold": config.actionability_rate_warn,
                }
            )
        if agg.trades_drift > config.trades_drift_alarm:
            alarms.append(
                {
                    "strategy": strategy,
                    "metric": "trades_drift",
                    "value": round(agg.trades_drift, 4),
                    "threshold": config.trades_drift_alarm,
                }
            )
        if agg.win_rate_drift > config.win_rate_drift_alarm:
            alarms.append(
                {
                    "strategy": strategy,
                    "metric": "win_rate_drift",
                    "value": round(agg.win_rate_drift, 4),
                    "threshold": config.win_rate_drift_alarm,
                }
            )

    return ReconciliationReport(
        config_summary={
            "backtest_run_id": config.backtest_run_id,
            "demo_window_start": config.demo_window_start.isoformat(),
            "demo_window_end": config.demo_window_end.isoformat(),
            "strategies": list(config.strategies),
        },
        aggregates=aggregates_payload,
        alarms=alarms,
        warnings=warnings,
    )


# ── CLI ───────────────────────────────────────────────────────


_DURATION_RE = re.compile(r"^(\d+)([dh])$")


def _parse_duration(text: str) -> timedelta:
    m = _DURATION_RE.match(text.strip().lower())
    if not m:
        raise ValueError(f"Invalid duration: {text!r}. Use formats like '7d' / '24h'.")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return timedelta(days=n)
    return timedelta(hours=n)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="demo_vs_backtest",
        description="Demo (db.demo) 实际成交 vs Backtest (db.live) 预期对账（ADR-010）",
    )
    parser.add_argument(
        "--backtest-run-id",
        required=True,
        help="db.live.backtest_runs 中的 run_id（baseline 预期）",
    )
    parser.add_argument(
        "--demo-window",
        default="7d",
        help="demo 评估时间窗口长度（默认 7d；支持 'Nd' / 'Nh'）",
    )
    parser.add_argument(
        "--demo-window-end",
        default=None,
        help="demo 窗口结束时间（ISO8601，默认 now UTC）",
    )
    parser.add_argument(
        "--strategies",
        required=True,
        help="逗号分隔的策略名（必填，避免误对账无关策略）",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        help="可选：把结构化结果写入此 JSON 文件",
    )
    parser.add_argument(
        "--signal-count-drift-alarm",
        type=float,
        default=0.30,
    )
    parser.add_argument(
        "--actionability-rate-warn",
        type=float,
        default=0.50,
    )
    parser.add_argument(
        "--trades-drift-alarm",
        type=float,
        default=0.30,
    )
    parser.add_argument(
        "--win-rate-drift-alarm",
        type=float,
        default=0.15,
        help="abs(demo_wr - backtest_wr) 超过此值即 ALARM（默认 0.15）",
    )
    args = parser.parse_args()

    try:
        window_delta = _parse_duration(args.demo_window)
    except ValueError as exc:
        logger.error(str(exc))
        return 2

    if args.demo_window_end:
        try:
            window_end = datetime.fromisoformat(args.demo_window_end)
        except ValueError:
            logger.error("--demo-window-end must be ISO8601")
            return 2
    else:
        window_end = datetime.now(timezone.utc)
    window_start = window_end - window_delta

    strategies = tuple(
        name.strip() for name in args.strategies.split(",") if name.strip()
    )
    if not strategies:
        logger.error("--strategies must contain at least one strategy name")
        return 2

    config = ReconciliationConfig(
        backtest_run_id=args.backtest_run_id,
        demo_window_start=window_start,
        demo_window_end=window_end,
        strategies=strategies,
        signal_count_drift_alarm=args.signal_count_drift_alarm,
        actionability_rate_warn=args.actionability_rate_warn,
        trades_drift_alarm=args.trades_drift_alarm,
        win_rate_drift_alarm=args.win_rate_drift_alarm,
    )

    try:
        aggregates = _load_backtest_metrics(args.backtest_run_id, strategies)
        _load_demo_aggregates(aggregates, window_start, window_end)
    except ValueError as exc:
        logger.error("Reconciliation aborted: %s", exc)
        return 2
    except Exception:
        logger.exception("Unexpected error loading reconciliation data")
        return 2

    report = _build_report(config, aggregates)
    payload = report.to_dict()
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("Wrote report to %s", args.json_output)

    if report.alarms:
        logger.error("ALARM: %d divergence(s) exceeded thresholds", len(report.alarms))
    if report.warnings:
        logger.warning(
            "WARN: %d divergence(s) flagged for attention", len(report.warnings)
        )
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
