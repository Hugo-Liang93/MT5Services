"""Execution Intent 端到端延迟分布探针。

回答三个问题（基于 execution_intents 表的真实历史）：
  1) published (created_at) → claimed (claimed_at) 延迟分布
     → 衡量 intent consumer 轮询间隔（poll_interval_seconds=0.5）的尾部影响
  2) claimed → completed 延迟分布
     → 衡量 MT5 下单往返 + pre-trade checks 的实际耗时
  3) published → completed 端到端延迟分布
     → 量化"信号→真实成交"总耗时，用于判断是否需降低 poll 间隔或改 LISTEN/NOTIFY

设计：
- 纯读 execution_intents 表，不触碰任何活线路。
- 按 target_account_key / strategy / timeframe 切分可选，默认全集。
- 输出 p50 / p90 / p95 / p99 / max，附样本数，支持 JSON。

示例：
    python -m src.ops.stress.intent_latency_probe --days 3
    python -m src.ops.stress.intent_latency_probe --days 7 --strategy breakout_follow
    python -m src.ops.stress.intent_latency_probe --days 14 --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from src.config import load_db_settings
from src.persistence.db import TimescaleWriter


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile（无外部依赖，避免 numpy）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return ordered[k]


def _summarize(label: str, samples: list[float]) -> dict[str, Any]:
    if not samples:
        return {
            "label": label,
            "samples": 0,
            "p50_ms": None,
            "p90_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
            "mean_ms": None,
        }
    return {
        "label": label,
        "samples": len(samples),
        "p50_ms": round(_percentile(samples, 50) * 1000, 2),
        "p90_ms": round(_percentile(samples, 90) * 1000, 2),
        "p95_ms": round(_percentile(samples, 95) * 1000, 2),
        "p99_ms": round(_percentile(samples, 99) * 1000, 2),
        "max_ms": round(max(samples) * 1000, 2),
        "mean_ms": round(statistics.fmean(samples) * 1000, 2),
    }


def _fetch_rows(
    writer: TimescaleWriter,
    *,
    since: datetime,
    strategy: str | None,
    target_account_key: str | None,
) -> Iterable[tuple[Any, ...]]:
    where = ["created_at >= %s"]
    params: list[Any] = [since]
    if strategy:
        where.append("strategy = %s")
        params.append(strategy)
    if target_account_key:
        where.append("target_account_key = %s")
        params.append(target_account_key)
    sql = f"""
        SELECT created_at, claimed_at, completed_at, status, strategy, timeframe
        FROM execution_intents
        WHERE {" AND ".join(where)}
    """
    with writer.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        yield from cur.fetchall()


def run(
    *,
    environment: str | None,
    days: int,
    strategy: str | None,
    target_account_key: str | None,
) -> dict[str, Any]:
    db_settings = load_db_settings(environment) if environment else load_db_settings()
    writer = TimescaleWriter(settings=db_settings, min_conn=1, max_conn=2)

    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))

    publish_to_claim: list[float] = []
    claim_to_complete: list[float] = []
    publish_to_complete: list[float] = []
    status_counts: dict[str, int] = {}
    by_tf: dict[str, list[float]] = {}

    for row in _fetch_rows(
        writer,
        since=since,
        strategy=strategy,
        target_account_key=target_account_key,
    ):
        created_at, claimed_at, completed_at, status, _strategy, timeframe = row
        status = str(status or "")
        status_counts[status] = status_counts.get(status, 0) + 1

        if created_at is None:
            continue
        tf = str(timeframe or "").upper() or "?"

        if claimed_at is not None:
            dt = (claimed_at - created_at).total_seconds()
            if dt >= 0:
                publish_to_claim.append(dt)
                by_tf.setdefault(tf, []).append(dt)
        if claimed_at is not None and completed_at is not None:
            dt = (completed_at - claimed_at).total_seconds()
            if dt >= 0:
                claim_to_complete.append(dt)
        if completed_at is not None:
            dt = (completed_at - created_at).total_seconds()
            if dt >= 0:
                publish_to_complete.append(dt)

    report = {
        "environment": environment or "default",
        "since_utc": since.isoformat(),
        "filter": {
            "strategy": strategy,
            "target_account_key": target_account_key,
        },
        "status_counts": status_counts,
        "publish_to_claim": _summarize("publish→claim", publish_to_claim),
        "claim_to_complete": _summarize("claim→complete", claim_to_complete),
        "publish_to_complete": _summarize("publish→complete", publish_to_complete),
        "publish_to_claim_by_tf": {
            tf: _summarize(tf, samples) for tf, samples in sorted(by_tf.items())
        },
    }
    return report


def _print_text(report: dict[str, Any]) -> None:
    print(f"Environment: {report['environment']}")
    print(f"Since UTC:   {report['since_utc']}")
    print(f"Status:      {report['status_counts']}")
    print()
    for key in ("publish_to_claim", "claim_to_complete", "publish_to_complete"):
        s = report[key]
        print(
            f"{s['label']:<22} "
            f"samples={s['samples']:<6} "
            f"p50={s['p50_ms']}ms "
            f"p95={s['p95_ms']}ms "
            f"p99={s['p99_ms']}ms "
            f"max={s['max_ms']}ms"
        )
    by_tf = report.get("publish_to_claim_by_tf") or {}
    if by_tf:
        print()
        print("publish→claim by timeframe:")
        for tf, s in by_tf.items():
            print(
                f"  {tf:<6} "
                f"samples={s['samples']:<6} "
                f"p50={s['p50_ms']}ms "
                f"p95={s['p95_ms']}ms "
                f"p99={s['p99_ms']}ms"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Execution intent latency probe")
    parser.add_argument("--environment", default=None, help="live / demo（留空=默认）")
    parser.add_argument("--days", type=int, default=3, help="回看天数")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--target-account-key", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = run(
        environment=args.environment,
        days=args.days,
        strategy=args.strategy,
        target_account_key=args.target_account_key,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_text(report)


if __name__ == "__main__":
    main()
