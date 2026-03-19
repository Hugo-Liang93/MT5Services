from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from .diagnostics import DiagnosticThresholds


class DiagnosticsEngine(Protocol):
    def build_report(
        self,
        rows: list[dict[str, Any]],
        *,
        symbol: str | None,
        timeframe: str | None,
        scope: str,
        thresholds: DiagnosticThresholds,
    ) -> dict[str, Any]:
        ...

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
        ...
