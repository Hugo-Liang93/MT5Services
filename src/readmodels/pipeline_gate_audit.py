from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _family_from_reason(reason: str) -> str:
    normalized = str(reason or "").strip()
    if not normalized:
        return "unknown"
    prefix, _, _ = normalized.partition(":")
    return prefix or normalized


class PipelineGateAuditReadModel:
    """基于 pipeline trace 的门禁拦截审计读模型。"""

    def __init__(self, *, pipeline_trace_repo: Any) -> None:
        self._pipeline_trace_repo = pipeline_trace_repo

    def summarize_gate_events(
        self,
        *,
        days: int = 7,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        symbol: str | None = None,
        timeframes: Sequence[str] | None = None,
        gate_families: Sequence[str] | None = None,
        sources: Sequence[str] | None = None,
        limit: int = 50000,
    ) -> dict[str, Any]:
        effective_to = _utc(to_time) or datetime.now(timezone.utc)
        effective_from = _utc(from_time) or (
            effective_to - timedelta(days=max(1, int(days)))
        )
        family_filter = {
            str(item).strip()
            for item in (gate_families or [])
            if str(item or "").strip()
        }
        source_filter = {
            str(item).strip()
            for item in (sources or [])
            if str(item or "").strip()
        }
        rows = self._pipeline_trace_repo.fetch_gate_events(
            from_time=effective_from,
            to_time=effective_to,
            symbol=symbol,
            timeframes=timeframes,
            limit=limit,
        )
        normalized_rows = [
            self._normalize_row(row)
            for row in rows
        ]
        normalized_rows = [
            row
            for row in normalized_rows
            if (not family_filter or row["gate_family"] in family_filter)
            and (not source_filter or row["gate_source"] in source_filter)
        ]

        period = {
            "from_time": effective_from.isoformat(),
            "to_time": effective_to.isoformat(),
            "days": round(
                (effective_to - effective_from).total_seconds() / 86400.0,
                4,
            ),
            "event_count": len(normalized_rows),
            "trace_count": len(
                {
                    str(row["trace_id"]).strip()
                    for row in normalized_rows
                    if str(row.get("trace_id") or "").strip()
                }
            ),
            "limit": int(limit),
            "truncated": len(rows) >= int(limit),
        }

        return {
            "period": period,
            "filters": {
                "symbol": symbol,
                "timeframes": list(timeframes or []),
                "gate_families": sorted(family_filter),
                "sources": sorted(source_filter),
            },
            "by_family": self._aggregate_by_key(
                normalized_rows,
                key_name="gate_family",
            ),
            "by_reason": self._aggregate_by_key(
                normalized_rows,
                key_name="gate_reason",
            ),
            "by_day": self._aggregate_by_day(normalized_rows),
            "by_source": self._aggregate_by_simple_key(
                normalized_rows,
                key_name="gate_source",
            ),
            "by_timeframe": self._aggregate_by_simple_key(
                normalized_rows,
                key_name="timeframe",
            ),
        }

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        gate_reason = str(row.get("gate_reason") or "").strip()
        gate_category = str(row.get("gate_category") or "").strip()
        gate_source = str(row.get("gate_source") or "").strip() or "unknown"
        evaluation_time = _utc(row.get("evaluation_time")) or _utc(
            row.get("recorded_at")
        )
        recorded_at = _utc(row.get("recorded_at"))
        timeframe = str(row.get("timeframe") or "").strip() or "unknown"
        scope = str(row.get("scope") or "").strip() or "unknown"
        trace_id = str(row.get("trace_id") or "").strip()
        return {
            "trace_id": trace_id,
            "symbol": str(row.get("symbol") or "").strip() or "unknown",
            "timeframe": timeframe,
            "scope": scope,
            "event_type": str(row.get("event_type") or "").strip() or "unknown",
            "gate_reason": gate_reason or "unknown",
            "gate_family": _family_from_reason(gate_reason),
            "gate_category": gate_category or _family_from_reason(gate_reason),
            "gate_source": gate_source,
            "recorded_at": recorded_at,
            "evaluation_time": evaluation_time,
            "evaluation_day": (
                evaluation_time.astimezone(timezone.utc).date().isoformat()
                if evaluation_time is not None
                else "unknown"
            ),
        }

    def _aggregate_by_key(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        key_name: str,
    ) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        total_events = 0
        for row in rows:
            total_events += 1
            key = str(row.get(key_name) or "unknown")
            bucket = buckets.setdefault(
                key,
                {
                    key_name: key,
                    "events": 0,
                    "trace_ids": set(),
                    "categories": Counter(),
                    "sources": Counter(),
                    "timeframes": Counter(),
                    "scopes": Counter(),
                    "first_seen": None,
                    "last_seen": None,
                },
            )
            bucket["events"] += 1
            if row["trace_id"]:
                bucket["trace_ids"].add(row["trace_id"])
            bucket["categories"][row["gate_category"]] += 1
            bucket["sources"][row["gate_source"]] += 1
            bucket["timeframes"][row["timeframe"]] += 1
            bucket["scopes"][row["scope"]] += 1
            seen_at = row["evaluation_time"] or row["recorded_at"]
            if seen_at is not None:
                if bucket["first_seen"] is None or seen_at < bucket["first_seen"]:
                    bucket["first_seen"] = seen_at
                if bucket["last_seen"] is None or seen_at > bucket["last_seen"]:
                    bucket["last_seen"] = seen_at

        result: list[dict[str, Any]] = []
        for bucket in buckets.values():
            events = int(bucket["events"])
            result.append(
                {
                    key_name: bucket[key_name],
                    "events": events,
                    "trace_count": len(bucket["trace_ids"]),
                    "share_pct": round(
                        (events / total_events) * 100.0,
                        2,
                    )
                    if total_events > 0
                    else 0.0,
                    "categories": dict(bucket["categories"]),
                    "sources": dict(bucket["sources"]),
                    "timeframes": dict(bucket["timeframes"]),
                    "scopes": dict(bucket["scopes"]),
                    "first_seen": (
                        bucket["first_seen"].isoformat()
                        if bucket["first_seen"] is not None
                        else None
                    ),
                    "last_seen": (
                        bucket["last_seen"].isoformat()
                        if bucket["last_seen"] is not None
                        else None
                    ),
                }
            )
        return sorted(result, key=lambda item: (-item["events"], item[key_name]))

    def _aggregate_by_day(self, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            day = str(row["evaluation_day"])
            bucket = buckets.setdefault(
                day,
                {
                    "day": day,
                    "events": 0,
                    "trace_ids": set(),
                    "families": Counter(),
                    "reasons": Counter(),
                    "sources": Counter(),
                },
            )
            bucket["events"] += 1
            if row["trace_id"]:
                bucket["trace_ids"].add(row["trace_id"])
            bucket["families"][row["gate_family"]] += 1
            bucket["reasons"][row["gate_reason"]] += 1
            bucket["sources"][row["gate_source"]] += 1

        result: list[dict[str, Any]] = []
        for bucket in buckets.values():
            result.append(
                {
                    "day": bucket["day"],
                    "events": int(bucket["events"]),
                    "trace_count": len(bucket["trace_ids"]),
                    "families": dict(bucket["families"]),
                    "reasons": dict(bucket["reasons"]),
                    "sources": dict(bucket["sources"]),
                }
            )
        return sorted(result, key=lambda item: item["day"])

    def _aggregate_by_simple_key(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        key_name: str,
    ) -> list[dict[str, Any]]:
        buckets: dict[str, set[str]] = defaultdict(set)
        counts: Counter[str] = Counter()
        for row in rows:
            key = str(row.get(key_name) or "unknown")
            counts[key] += 1
            if row["trace_id"]:
                buckets[key].add(row["trace_id"])
        return [
            {
                key_name: key,
                "events": count,
                "trace_count": len(buckets.get(key, set())),
            }
            for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

