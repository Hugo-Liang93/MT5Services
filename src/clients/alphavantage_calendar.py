"""Alpha Vantage 经济指标客户端。

API 文档：https://www.alphavantage.co/documentation/
免费层：25 请求/天

注意：Alpha Vantage 返回的是经济指标时间序列（非日历事件格式），
需要与其他日历源的事件做交叉关联来补充 actual/previous 值。
本客户端将每个指标的最新数据点转换为 EconomicCalendarEvent。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional

from src.clients.economic_calendar import (
    EconomicCalendarEvent,
    _BaseHttpClient,
    _as_text,
)

logger = logging.getLogger(__name__)

# Alpha Vantage function → 人类可读名称映射
_INDICATOR_NAMES: Dict[str, str] = {
    "REAL_GDP": "Real GDP",
    "CPI": "Consumer Price Index",
    "INFLATION": "Inflation Rate",
    "RETAIL_SALES": "Retail Sales",
    "FEDERAL_FUNDS_RATE": "Federal Funds Rate",
    "NONFARM_PAYROLL": "Nonfarm Payrolls",
    "UNEMPLOYMENT": "Unemployment Rate",
    "TREASURY_YIELD": "Treasury Yield",
    "CONSUMER_SENTIMENT": "Consumer Sentiment",
}

# 各指标的重要性等级（对 XAUUSD 的影响程度）
_INDICATOR_IMPORTANCE: Dict[str, int] = {
    "REAL_GDP": 3,
    "CPI": 3,
    "NONFARM_PAYROLL": 3,
    "FEDERAL_FUNDS_RATE": 3,
    "UNEMPLOYMENT": 3,
    "INFLATION": 2,
    "RETAIL_SALES": 2,
    "TREASURY_YIELD": 2,
    "CONSUMER_SENTIMENT": 2,
}

# 各指标的默认查询 interval
_INDICATOR_INTERVALS: Dict[str, str] = {
    "REAL_GDP": "quarterly",
    "CPI": "monthly",
    "INFLATION": "monthly",
    "RETAIL_SALES": "monthly",
    "FEDERAL_FUNDS_RATE": "monthly",
    "NONFARM_PAYROLL": "monthly",
    "UNEMPLOYMENT": "monthly",
    "TREASURY_YIELD": "monthly",
    "CONSUMER_SENTIMENT": "monthly",
}


class AlphaVantageClient(_BaseHttpClient):
    base_url = "https://www.alphavantage.co/query"

    @property
    def name(self) -> str:
        return "alphavantage"

    def supports_release_watch(self) -> bool:
        return False

    def is_configured(self) -> bool:
        return bool(self.settings.alphavantage_api_key)

    def _tracked_indicators(self) -> List[str]:
        configured = getattr(self.settings, "alphavantage_tracked_indicators", None)
        if configured:
            return [ind.strip().upper() for ind in configured if ind.strip()]
        return list(_INDICATOR_NAMES.keys())

    def _fetch_indicator(self, function: str) -> List[Dict[str, Any]]:
        api_key = self.settings.alphavantage_api_key
        if not api_key:
            return []

        interval = _INDICATOR_INTERVALS.get(function, "monthly")
        params: Dict[str, Any] = {
            "function": function,
            "apikey": api_key,
        }
        if function not in ("INFLATION",):
            params["interval"] = interval

        response = self._request_json(self.base_url, params)
        if not isinstance(response, dict):
            return []

        return response.get("data", [])

    def fetch_events(
        self,
        start_date: date,
        end_date: date,
        countries: Optional[List[str]] = None,
    ) -> List[EconomicCalendarEvent]:
        if not self.is_configured():
            return []

        events: List[EconomicCalendarEvent] = []
        for function in self._tracked_indicators():
            try:
                data_points = self._fetch_indicator(function)
                event_name = _INDICATOR_NAMES.get(function, function)

                for point in data_points:
                    try:
                        date_str = point.get("date")
                        if not date_str:
                            continue
                        point_date = date.fromisoformat(date_str)
                        if not (start_date <= point_date <= end_date):
                            continue

                        scheduled_at = datetime.combine(
                            point_date, time.min, tzinfo=timezone.utc
                        )
                        value = _as_text(point.get("value"))
                        if value == ".":
                            value = None

                        event_uid = f"alphavantage:{function}:{date_str}"
                        # 根据指标类型分配合理的重要性等级
                        imp = _INDICATOR_IMPORTANCE.get(function, 2)
                        events.append(
                            EconomicCalendarEvent(
                                scheduled_at=scheduled_at,
                                event_uid=event_uid,
                                source="alphavantage",
                                provider_event_id=f"{function}:{date_str}",
                                event_name=event_name,
                                country="United States",
                                category="economic_indicator",
                                currency="USD",
                                reference=date_str,
                                actual=value,
                                importance=imp,
                                all_day=True,
                                raw_payload=point,
                            )
                        )
                    except Exception:
                        logger.exception(
                            "Failed to normalize AV data point for %s: %s",
                            function,
                            point,
                        )
            except Exception:
                logger.exception("Failed to fetch AV indicator: %s", function)

        return events
