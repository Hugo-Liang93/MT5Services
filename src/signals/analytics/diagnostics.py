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
