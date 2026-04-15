"""基于历史 intrabar 信号流重放 IntrabarTradeCoordinator，扫描 min_stable_bars。

数据源：signal_preview_events（scope=intrabar 的真实策略输出）。
模拟：按 (symbol, parent_tf, strategy) 分组、按 generated_at 排序，
      把 direction/confidence/bar_time 喂给 IntrabarTradeCoordinator，
      统计在不同 min_stable_bars ∈ {2, 3, 4} 下 armed 事件数与首次 armed 延迟。

为什么重要：
  - 当前 min_stable_bars=3 + 子 TF(如 M5) 触发意味着父 TF(H1) 进入"可盘中交易"
    最少需要 3 × 5min = 15min。这段延迟压缩了盘中入场的价值。
  - 降到 2 能提前 5min 但可能提升误触发率；升到 4 更稳但更慢。
  - 此脚本给出历史分布上的定量 trade-off，供参数决策。

输出：
  - 每个 N 值的 armed 次数、armed_rate = armed / evaluations
  - 首次 armed 距离 parent bar 起点的延迟分布
  - 按策略/TF 切分
  - JSON 可选

示例：
    python -m src.ops.stress.replay_intrabar --days 7
    python -m src.ops.stress.replay_intrabar --days 14 --tf H1 --json
    python -m src.ops.stress.replay_intrabar --strategy breakout_follow --days 30
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import load_db_settings
from src.persistence.db import TimescaleWriter
from src.signals.orchestration.intrabar_trade_coordinator import (
    IntrabarTradeCoordinator,
    IntrabarTradingPolicy,
)


# 父 TF → 秒数（用于把 generated_at 对齐到 parent bar 时间）
TF_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


def _truncate_to_parent_bar(ts: datetime, parent_tf: str) -> datetime:
    """将时间戳对齐到 parent TF bar 的起点。"""
    seconds = TF_SECONDS.get(parent_tf.upper(), 3600)
    epoch = int(ts.timestamp())
    aligned = epoch - (epoch % seconds)
    return datetime.fromtimestamp(aligned, tz=timezone.utc)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return ordered[k]


def _fetch_preview_events(
    writer: TimescaleWriter,
    *,
    since: datetime,
    tf: str | None,
    strategy: str | None,
) -> list[tuple[Any, ...]]:
    where = ["generated_at >= %s", "direction IN ('buy', 'sell')"]
    params: list[Any] = [since]
    if tf:
        where.append("timeframe = %s")
        params.append(tf.upper())
    if strategy:
        where.append("strategy = %s")
        params.append(strategy)
    sql = f"""
        SELECT generated_at, symbol, timeframe, strategy, direction, confidence
        FROM signal_preview_events
        WHERE {" AND ".join(where)}
        ORDER BY generated_at ASC
    """
    with writer.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _simulate(
    events: list[tuple[Any, ...]],
    *,
    min_stable_bars: int,
    min_confidence: float,
) -> dict[str, Any]:
    """用给定参数重放，返回 armed 统计。"""
    # 收集所有 strategy 进白名单（简化：全部允许）
    strategies = {str(r[3]) for r in events}
    policy = IntrabarTradingPolicy(
        min_stable_bars=min_stable_bars,
        min_confidence=min_confidence,
        enabled_strategies=frozenset(strategies),
    )
    coordinator = IntrabarTradeCoordinator(policy)

    armed_count = 0
    eval_count = 0
    armed_by_tf: dict[str, int] = defaultdict(int)
    armed_by_strategy: dict[str, int] = defaultdict(int)
    # 首次 armed 距 parent bar 起点的延迟（秒）
    armed_latency: list[float] = []

    # 跟踪每个 key 的"parent bar 起点时间"以计算首次 armed 延迟
    bar_start_of: dict[tuple[str, str, str], datetime] = {}

    for generated_at, symbol, timeframe, strategy, direction, confidence in events:
        eval_count += 1
        parent_tf = str(timeframe).upper()
        parent_bar_time = _truncate_to_parent_bar(generated_at, parent_tf)
        key = (str(symbol), parent_tf, str(strategy))

        # 检测新 parent bar
        if bar_start_of.get(key) != parent_bar_time:
            bar_start_of[key] = parent_bar_time

        state = coordinator.update(
            symbol=str(symbol),
            parent_tf=parent_tf,
            strategy=str(strategy),
            direction=str(direction),
            confidence=float(confidence or 0.0),
            parent_bar_time=parent_bar_time,
        )
        if state and state.startswith("intrabar_armed_"):
            armed_count += 1
            armed_by_tf[parent_tf] += 1
            armed_by_strategy[str(strategy)] += 1
            delay = (generated_at - parent_bar_time).total_seconds()
            if delay >= 0:
                armed_latency.append(delay)

    return {
        "min_stable_bars": min_stable_bars,
        "min_confidence": min_confidence,
        "evaluations": eval_count,
        "armed": armed_count,
        "armed_rate_pct": (
            round(100.0 * armed_count / eval_count, 3) if eval_count else 0.0
        ),
        "armed_by_tf": dict(armed_by_tf),
        "armed_by_strategy": dict(armed_by_strategy),
        "latency_from_bar_start_s": {
            "p50": round(_percentile(armed_latency, 50), 1),
            "p95": round(_percentile(armed_latency, 95), 1),
            "max": round(max(armed_latency) if armed_latency else 0.0, 1),
        },
    }


def run(
    *,
    environment: str | None,
    days: int,
    tf: str | None,
    strategy: str | None,
    min_confidence: float,
    sweep: list[int],
) -> dict[str, Any]:
    db_settings = load_db_settings(environment) if environment else load_db_settings()
    writer = TimescaleWriter(settings=db_settings, min_conn=1, max_conn=2)

    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    events = _fetch_preview_events(writer, since=since, tf=tf, strategy=strategy)

    results = [
        _simulate(events, min_stable_bars=n, min_confidence=min_confidence)
        for n in sweep
    ]

    return {
        "environment": environment or "default",
        "since_utc": since.isoformat(),
        "filter": {"tf": tf, "strategy": strategy},
        "min_confidence": min_confidence,
        "total_preview_events": len(events),
        "sweep": results,
    }


def _print_text(report: dict[str, Any]) -> None:
    print(f"Environment: {report['environment']}")
    print(f"Since UTC:   {report['since_utc']}")
    print(f"Filter:      {report['filter']}")
    print(f"Events:      {report['total_preview_events']}")
    print(f"min_confidence = {report['min_confidence']}")
    print()
    header = f"{'N':<4} {'armed':<8} {'armed_rate':<12} {'p50 lat(s)':<12} {'p95 lat(s)':<12} {'max lat(s)':<12}"
    print(header)
    print("-" * len(header))
    for r in report["sweep"]:
        lat = r["latency_from_bar_start_s"]
        print(
            f"{r['min_stable_bars']:<4} "
            f"{r['armed']:<8} "
            f"{r['armed_rate_pct']:<12} "
            f"{lat['p50']:<12} "
            f"{lat['p95']:<12} "
            f"{lat['max']:<12}"
        )
    # 按 TF 展开
    print()
    print("armed counts by tf per N:")
    for r in report["sweep"]:
        print(f"  N={r['min_stable_bars']}: {r['armed_by_tf']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay intrabar stability sweep")
    parser.add_argument("--environment", default=None)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--tf", default=None, help="过滤父 TF（如 H1）")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument(
        "--sweep",
        default="2,3,4",
        help="扫描的 min_stable_bars 值，逗号分隔",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    sweep = [int(x.strip()) for x in args.sweep.split(",") if x.strip()]
    report = run(
        environment=args.environment,
        days=args.days,
        tf=args.tf,
        strategy=args.strategy,
        min_confidence=args.min_confidence,
        sweep=sweep,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_text(report)


if __name__ == "__main__":
    main()
