from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..contracts import resolve_session_by_hour
from ..metadata_keys import MetadataKey as MK
from .plugins import AnalyticsPluginRegistry


@dataclass(frozen=True)
class DiagnosticThresholds:
    conflict_warn_threshold: float = 0.35
    hold_warn_threshold: float = 0.75
    confidence_warn_threshold: float = 0.45


class SignalDiagnosticsAnalyzer:
    """纯分析层：负责把 recent signals 聚合成诊断报告。"""

    def __init__(self, plugin_registry: AnalyticsPluginRegistry | None = None):
        self._plugin_registry = plugin_registry or AnalyticsPluginRegistry()

    @staticmethod
    def signal_bar_key(row: dict[str, Any]) -> str:
        metadata = row.get("metadata")
        if isinstance(metadata, dict):
            bar_time = metadata.get(MK.BAR_TIME)
            if isinstance(bar_time, str) and bar_time.strip():
                return bar_time
        generated_at = row.get("generated_at")
        if isinstance(generated_at, str) and generated_at.strip():
            return generated_at
        return "unknown"

    def build_report(
        self,
        rows: list[dict[str, Any]],
        *,
        symbol: str | None,
        timeframe: str | None,
        scope: str,
        thresholds: DiagnosticThresholds,
    ) -> dict[str, Any]:
        strategy_action_counter: dict[str, Counter[str]] = defaultdict(Counter)
        strategy_confidence_sum: dict[str, float] = defaultdict(float)
        strategy_rows: dict[str, int] = defaultdict(int)
        strategy_missing_required: Counter[str] = Counter()
        regime_counter: Counter[str] = Counter()
        session_counter: Counter[str] = Counter()
        bars: dict[str, list[str]] = defaultdict(list)
        strategy_session_counter: dict[str, Counter[str]] = defaultdict(Counter)

        for row in rows:
            strategy = str(row.get("strategy") or "unknown")
            action = str(row.get("direction") or "hold")
            confidence_raw = row.get("confidence")
            confidence = (
                float(confidence_raw)
                if isinstance(confidence_raw, (int, float))
                else 0.0
            )
            session = self._resolve_session(row)

            strategy_action_counter[strategy][action] += 1
            strategy_confidence_sum[strategy] += confidence
            strategy_rows[strategy] += 1
            strategy_session_counter[strategy][session] += 1
            bars[self.signal_bar_key(row)].append(action)
            session_counter[session] += 1

            reason = str(row.get("reason") or "")
            if reason.startswith("missing_required_indicator") or reason.startswith(
                "missing_required_indicators"
            ):
                strategy_missing_required[strategy] += 1

            metadata = row.get("metadata")
            if isinstance(metadata, dict):
                regime = metadata.get(MK.REGIME)
                if isinstance(regime, str) and regime.strip():
                    regime_counter[regime] += 1

        conflicting_bars = 0
        executable_bars = 0
        for actions in bars.values():
            non_hold = {a for a in actions if a in {"buy", "sell"}}
            if non_hold:
                executable_bars += 1
            if "buy" in non_hold and "sell" in non_hold:
                conflicting_bars += 1

        strategy_breakdown: list[dict[str, Any]] = []
        strategy_health: list[dict[str, Any]] = []
        for strategy in sorted(strategy_rows.keys()):
            total = strategy_rows[strategy]
            actions = strategy_action_counter[strategy]
            hold_count = actions.get("hold", 0)
            hold_ratio = hold_count / total if total else 0.0
            avg_confidence = strategy_confidence_sum[strategy] / total if total else 0.0
            missing_required = strategy_missing_required.get(strategy, 0)
            strategy_breakdown.append(
                {
                    "strategy": strategy,
                    "total_signals": total,
                    "buy_count": actions.get("buy", 0),
                    "sell_count": actions.get("sell", 0),
                    "hold_count": hold_count,
                    "hold_ratio": hold_ratio,
                    "avg_confidence": avg_confidence,
                    "missing_required_count": missing_required,
                }
            )
            warnings: list[str] = []
            if hold_ratio >= thresholds.hold_warn_threshold:
                warnings.append("high_hold_ratio")
            if avg_confidence <= thresholds.confidence_warn_threshold:
                warnings.append("low_avg_confidence")
            if missing_required > 0:
                warnings.append("missing_required_indicator")
            strategy_health.append(
                {
                    "strategy": strategy,
                    "warnings": warnings,
                    "status": "warn" if warnings else "ok",
                }
            )

        conflict_ratio = conflicting_bars / executable_bars if executable_bars else 0.0
        top_regime = regime_counter.most_common(1)[0][0] if regime_counter else None
        recommendations: list[str] = []
        if conflict_ratio >= thresholds.conflict_warn_threshold:
            recommendations.append(
                "conflict_ratio_high: split trend/timing strategy groups and tighten consensus gating"
            )
        if any(item["status"] == "warn" for item in strategy_health):
            recommendations.append(
                "strategy_health_warn: prioritize strategies with high hold ratio / low confidence / missing indicators"
            )

        report = {
            "rows_analyzed": len(rows),
            "symbol": symbol,
            "timeframe": timeframe,
            "scope": scope,
            "thresholds": {
                "conflict_warn_threshold": thresholds.conflict_warn_threshold,
                "hold_warn_threshold": thresholds.hold_warn_threshold,
                "confidence_warn_threshold": thresholds.confidence_warn_threshold,
            },
            "conflict": {
                "bars_total": len(bars),
                "bars_with_executable_signals": executable_bars,
                "bars_with_buy_sell_conflict": conflicting_bars,
                "conflict_ratio": conflict_ratio,
            },
            "dominant_regime": top_regime,
            "regime_distribution": dict(regime_counter),
            "session_distribution": dict(session_counter),
            "strategy_breakdown": strategy_breakdown,
            "strategy_session_breakdown": {
                strategy_name: dict(counter)
                for strategy_name, counter in strategy_session_counter.items()
            },
            "strategy_health": strategy_health,
            "recommendations": recommendations,
        }
        report["extensions"] = self._plugin_registry.apply(
            rows=rows, base_report=report
        )
        return report

    def build_daily_quality_report(
        self,
        rows: list[dict[str, Any]],
        *,
        symbol: str | None,
        timeframe: str | None,
        scope: str,
        thresholds: DiagnosticThresholds,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """生成过去 24 小时的信号质量日报。"""
        now_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
        window_start = now_utc - timedelta(hours=24)

        filtered_rows: list[dict[str, Any]] = []
        skipped_rows_invalid_time = 0
        for row in rows:
            dt = self._extract_datetime(self.signal_bar_key(row))
            if dt is None:
                skipped_rows_invalid_time += 1
                continue
            if window_start <= dt <= now_utc:
                filtered_rows.append(row)

        base = self.build_report(
            filtered_rows,
            symbol=symbol,
            timeframe=timeframe,
            scope=scope,
            thresholds=thresholds,
        )
        warning_rank = sorted(
            base["strategy_health"],
            key=lambda item: len(item.get("warnings", [])),
            reverse=True,
        )
        top_warning_strategies = [
            item for item in warning_rank if item.get("warnings")
        ][:3]
        report = {
            "generated_at": now_utc.isoformat(),
            "window": {
                "start": window_start.isoformat(),
                "end": now_utc.isoformat(),
                "hours": 24,
            },
            "symbol": symbol,
            "timeframe": timeframe,
            "scope": scope,
            "rows_analyzed": base["rows_analyzed"],
            "rows_before_window_filter": len(rows),
            "skipped_rows_invalid_time": skipped_rows_invalid_time,
            "conflict": base["conflict"],
            "dominant_regime": base["dominant_regime"],
            "session_distribution": base["session_distribution"],
            "top_warning_strategies": top_warning_strategies,
            "recommendations": base["recommendations"],
        }
        report["extensions"] = self._plugin_registry.apply(
            rows=filtered_rows, base_report=report
        )
        return report

    def build_strategy_audit_report(
        self,
        rows: list[dict[str, Any]],
        *,
        scope: str = "confirmed",
        symbol: str | None = None,
        timeframe: str | None = None,
        winrate_rows: list[dict[str, Any]] | None = None,
        strategy_categories: dict[str, str] | None = None,
        thresholds: DiagnosticThresholds | None = None,
    ) -> dict[str, Any]:
        """单端点回答 backlog P0.3：每策略 admission/conflict/winrate/状态聚合。

        字段对齐 docs/quantx-backend-backlog.md §P0.3：
            strategy / category / signals / actionable_signals / hold_count /
            blocked_count / conflict_count / hold_rate / blocked_rate /
            conflict_rate / avg_confidence / win_rate / last_signal_at /
            recent_issue / status
        """
        thresholds = thresholds or DiagnosticThresholds()
        winrate_by_strategy = _aggregate_winrates_by_strategy(winrate_rows or [])
        categories = strategy_categories or {}

        # 按 strategy 分组
        strategy_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            strategy = str(row.get("strategy") or "unknown")
            strategy_rows[strategy].append(row)

        # 计算每个 bar 的 strategy 集合（用于 conflict 检测：同一 bar 上 buy/sell 双向）
        bar_actions: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        for row in rows:
            bar_key = self.signal_bar_key(row)
            strategy = str(row.get("strategy") or "unknown")
            direction = str(row.get("direction") or "").lower()
            if direction in ("buy", "sell"):
                bar_actions[bar_key][strategy].add(direction)

        strategies_payload: list[dict[str, Any]] = []
        for strategy in sorted(strategy_rows.keys()):
            items = strategy_rows[strategy]
            total = len(items)
            actionable = sum(1 for r in items if r.get("actionability") == "actionable")
            blocked = sum(1 for r in items if r.get("actionability") == "blocked")
            # hold = 显式 hold + 旧记录 actionability=None（未到 executor）
            hold = total - actionable - blocked
            conflict_count = sum(
                1
                for bar, by_strategy in bar_actions.items()
                if "buy" in by_strategy.get(strategy, set())
                and "sell" in by_strategy.get(strategy, set())
            )
            confidence_sum = sum(_safe_float(r.get("confidence")) for r in items)
            avg_confidence = confidence_sum / total if total else 0.0
            last_signal_at = (
                max(
                    (str(r.get("generated_at") or "") for r in items),
                    default="",
                )
                or None
            )
            hold_rate = hold / total if total else 0.0
            blocked_rate = blocked / total if total else 0.0
            conflict_rate = conflict_count / total if total else 0.0

            warnings: list[str] = []
            if hold_rate >= thresholds.hold_warn_threshold:
                warnings.append("high_hold_ratio")
            if blocked_rate >= 0.5:
                warnings.append("high_blocked_ratio")
            if avg_confidence <= thresholds.confidence_warn_threshold:
                warnings.append("low_avg_confidence")
            if conflict_rate >= thresholds.conflict_warn_threshold:
                warnings.append("high_conflict_ratio")

            recent_issue = warnings[0] if warnings else None
            status = "warn" if warnings else "ok"

            strategies_payload.append(
                {
                    "strategy": strategy,
                    "category": categories.get(strategy),
                    "signals": total,
                    "actionable_signals": actionable,
                    "hold_count": hold,
                    "blocked_count": blocked,
                    "conflict_count": conflict_count,
                    "hold_rate": round(hold_rate, 4),
                    "blocked_rate": round(blocked_rate, 4),
                    "conflict_rate": round(conflict_rate, 4),
                    "avg_confidence": round(avg_confidence, 4),
                    "win_rate": winrate_by_strategy.get(strategy),
                    "last_signal_at": last_signal_at,
                    "recent_issue": recent_issue,
                    "status": status,
                    "warnings": list(warnings),
                }
            )

        return {
            "rows_analyzed": len(rows),
            "scope": scope,
            "symbol": symbol,
            "timeframe": timeframe,
            "thresholds": {
                "conflict_warn_threshold": thresholds.conflict_warn_threshold,
                "hold_warn_threshold": thresholds.hold_warn_threshold,
                "confidence_warn_threshold": thresholds.confidence_warn_threshold,
            },
            "strategies": strategies_payload,
        }

    @staticmethod
    def _resolve_session(row: dict[str, Any]) -> str:
        timestamp = SignalDiagnosticsAnalyzer.signal_bar_key(row)
        hour = SignalDiagnosticsAnalyzer._extract_hour(timestamp)
        return resolve_session_by_hour(hour)

    @staticmethod
    def _extract_hour(timestamp: str) -> int | None:
        dt = SignalDiagnosticsAnalyzer._extract_datetime(timestamp)
        if dt is None:
            return None
        return dt.hour

    @staticmethod
    def _extract_datetime(timestamp: str) -> datetime | None:
        try:
            normalized = timestamp.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None


# ── Helpers for build_strategy_audit_report ─────────────────────


def _safe_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _aggregate_winrates_by_strategy(
    winrate_rows: list[dict[str, Any]],
) -> dict[str, float | None]:
    """把 strategy_winrates() 的 (strategy, direction) 行按 strategy 加权平均。

    strategy_winrates 返回 list[{strategy, direction, total, wins, win_rate, ...}]，
    需要按 strategy 聚合：sum(wins) / sum(total)。total=0 时返回 None。
    """
    by_strategy: dict[str, dict[str, float]] = defaultdict(
        lambda: {"total": 0.0, "wins": 0.0}
    )
    for row in winrate_rows:
        strategy = str(row.get("strategy") or "")
        if not strategy:
            continue
        bucket = by_strategy[strategy]
        bucket["total"] += _safe_float(row.get("total"))
        bucket["wins"] += _safe_float(row.get("wins"))
    out: dict[str, float | None] = {}
    for strategy, bucket in by_strategy.items():
        if bucket["total"] <= 0:
            out[strategy] = None
        else:
            out[strategy] = round(bucket["wins"] / bucket["total"], 4)
    return out
