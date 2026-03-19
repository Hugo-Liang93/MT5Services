from __future__ import annotations

from typing import Any, Callable

MetricCalculator = Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]]


class AnalyticsPluginRegistry:
    """用于扩展 diagnostics 的插件注册表。"""

    def __init__(self) -> None:
        self._plugins: dict[str, MetricCalculator] = {}

    def register(self, name: str, calculator: MetricCalculator) -> None:
        self._plugins[name] = calculator

    def apply(
        self,
        *,
        rows: list[dict[str, Any]],
        base_report: dict[str, Any],
    ) -> dict[str, Any]:
        extensions: dict[str, Any] = {}
        for name, calculator in self._plugins.items():
            try:
                extensions[name] = calculator(rows, base_report)
            except Exception as exc:
                extensions[name] = {"error": str(exc)}
        return extensions
