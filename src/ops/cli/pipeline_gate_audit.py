"""Pipeline 门禁拦截审计 CLI。

用途：
- 在系统运行数天后，汇总 session / market-data / execution gate 的真实拦截分布
- 避免临时翻库、重复拼 SQL

示例：
    python -m src.ops.cli.pipeline_gate_audit --environment live --days 5
    python -m src.ops.cli.pipeline_gate_audit --environment live --days 7 --focus session_transition_cooldown,quote_stale
    python -m src.ops.cli.pipeline_gate_audit --environment live --days 14 --json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Sequence

from src.config import load_db_settings
from src.persistence.db import TimescaleWriter
from src.readmodels import PipelineGateAuditReadModel


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _print_header(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _print_ranked(
    title: str,
    items: Sequence[dict[str, object]],
    *,
    key_name: str,
    limit: int = 10,
) -> None:
    _print_header(title)
    if not items:
        print("无数据")
        return
    for idx, item in enumerate(items[:limit], 1):
        print(
            f"{idx:>2}. {item[key_name]} | events={item['events']} | "
            f"traces={item['trace_count']} | share={item.get('share_pct', 0.0)}%"
        )
        categories = item.get("categories") or {}
        if categories:
            print(f"    categories={json.dumps(categories, ensure_ascii=False, sort_keys=True)}")
        timeframes = item.get("timeframes") or {}
        if timeframes:
            print(f"    timeframes={json.dumps(timeframes, ensure_ascii=False, sort_keys=True)}")
        sources = item.get("sources") or {}
        if sources:
            print(f"    sources={json.dumps(sources, ensure_ascii=False, sort_keys=True)}")


def _print_by_day(items: Sequence[dict[str, object]], *, focus: Sequence[str]) -> None:
    _print_header("按日分布")
    if not items:
        print("无数据")
        return
    normalized_focus = {str(item).strip() for item in focus if str(item).strip()}
    for item in items:
        families = item.get("families") or {}
        focus_counts = {
            family: count
            for family, count in dict(families).items()
            if not normalized_focus or family in normalized_focus
        }
        print(
            f"{item['day']} | events={item['events']} | traces={item['trace_count']} | "
            f"focus={json.dumps(focus_counts, ensure_ascii=False, sort_keys=True)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit pipeline gate events over recent days")
    parser.add_argument(
        "--environment",
        default="live",
        choices=["live", "demo"],
        help="数据库环境",
    )
    parser.add_argument("--days", type=int, default=7, help="回看天数")
    parser.add_argument("--symbol", default=None, help="按 symbol 过滤")
    parser.add_argument(
        "--timeframes",
        default="",
        help="按 timeframe 过滤，逗号分隔，如 M1,M5,M15",
    )
    parser.add_argument(
        "--focus",
        default="session_transition_cooldown,quote_stale,intrabar_synthesis_stale,intrabar_synthesis_unavailable",
        help="重点关注的 gate family，逗号分隔",
    )
    parser.add_argument(
        "--sources",
        default="",
        help="按 source 过滤，逗号分隔：signal_filter,execution",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50000,
        help="最多读取多少条门禁事件",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON",
    )
    args = parser.parse_args()

    timeframes = _parse_csv(args.timeframes)
    focus = _parse_csv(args.focus)
    sources = _parse_csv(args.sources)

    writer = TimescaleWriter(load_db_settings(args.environment))
    try:
        read_model = PipelineGateAuditReadModel(
            pipeline_trace_repo=writer.pipeline_trace_repo
        )
        summary = read_model.summarize_gate_events(
            days=max(1, int(args.days)),
            symbol=args.symbol,
            timeframes=timeframes,
            sources=sources,
            limit=max(1, int(args.limit)),
        )
    finally:
        writer.close()

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return

    period = summary["period"]
    print("Pipeline Gate Audit")
    print("===================")
    print(
        f"环境={args.environment} | from={period['from_time']} | to={period['to_time']} | "
        f"events={period['event_count']} | traces={period['trace_count']} | "
        f"limit={period['limit']} | truncated={period['truncated']}"
    )
    if args.symbol:
        print(f"symbol={args.symbol}")
    if timeframes:
        print(f"timeframes={','.join(timeframes)}")
    if sources:
        print(f"sources={','.join(sources)}")
    print(f"generated_at={datetime.now(timezone.utc).isoformat()}")

    _print_ranked("Top Gate Family", summary["by_family"], key_name="gate_family")
    _print_ranked("Top Gate Reason", summary["by_reason"], key_name="gate_reason")

    focus_buckets = [
        item
        for item in summary["by_family"]
        if item["gate_family"] in set(focus)
    ]
    _print_ranked("Focus Family", focus_buckets, key_name="gate_family", limit=len(focus) or 10)
    _print_by_day(summary["by_day"], focus=focus)


if __name__ == "__main__":
    main()

