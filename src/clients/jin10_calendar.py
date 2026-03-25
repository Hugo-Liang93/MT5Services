"""金十数据 (Jin10) 经济日历客户端。

数据来源：https://rili.jin10.com/
API 端点：z3c.jin10.com（阿里云 ALB，无需 API Key）
免费、无配额限制，合理使用即可。

数据特点：
- 覆盖全球主要经济体，含 1-5 星重要性分级
- pub_time 为北京时间（Asia/Shanghai），需转 UTC
- 免费版的 actual/consensus/previous 数值可能被锁定（lock=true）
- 日历结构（时间 + 国家 + 星级）始终可用，满足 Trade Guard 需求
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.clients.economic_calendar import (
    EconomicCalendarEvent,
    EconomicCalendarError,
    _BaseHttpClient,
    _as_text,
    _event_in_date_range,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://e0430d16720e4211b5e072c26205c890.z3c.jin10.com"

_BEIJING_OFFSET = timezone(timedelta(hours=8))

# 金十中文国家名 → 英文（与 EconomicCalendarEvent.country / COUNTRY_TIMEZONE_MAP 对齐）
_COUNTRY_MAP: Dict[str, str] = {
    "美国": "United States",
    "中国": "China",
    "欧元区": "Euro Area",
    "英国": "United Kingdom",
    "日本": "Japan",
    "德国": "Germany",
    "法国": "France",
    "意大利": "Italy",
    "西班牙": "Spain",
    "加拿大": "Canada",
    "澳大利亚": "Australia",
    "新西兰": "New Zealand",
    "瑞士": "Switzerland",
    "韩国": "South Korea",
    "印度": "India",
    "巴西": "Brazil",
    "墨西哥": "Mexico",
    "俄罗斯": "Russia",
    "南非": "South Africa",
    "土耳其": "Turkey",
    "瑞典": "Sweden",
    "挪威": "Norway",
    "丹麦": "Denmark",
    "新加坡": "Singapore",
    "泰国": "Thailand",
    "印度尼西亚": "Indonesia",
    "马来西亚": "Malaysia",
    "菲律宾": "Philippines",
    "波兰": "Poland",
    "以色列": "Israel",
    "沙特阿拉伯": "Saudi Arabia",
    "阿根廷": "Argentina",
    "智利": "Chile",
    "哥伦比亚": "Colombia",
    "中国香港": "Hong Kong",
    "中国台湾": "Taiwan",
}

# 金十中文国家名 → 主要货币
_COUNTRY_CURRENCY: Dict[str, str] = {
    "美国": "USD",
    "欧元区": "EUR",
    "英国": "GBP",
    "日本": "JPY",
    "加拿大": "CAD",
    "澳大利亚": "AUD",
    "新西兰": "NZD",
    "瑞士": "CHF",
    "中国": "CNY",
}

# 金十 star(1-5) → importance(1-3) 映射
_STAR_TO_IMPORTANCE: Dict[int, int] = {
    1: 1,
    2: 1,
    3: 2,
    4: 3,
    5: 3,
}


def _parse_beijing_time(pub_time: str) -> datetime:
    """解析金十的北京时间字符串，返回 UTC datetime。"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(pub_time, fmt)
            if dt.year < 2000 or dt.year > 2100:
                continue
            return dt.replace(tzinfo=_BEIJING_OFFSET).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse Jin10 pub_time: {pub_time}")


class Jin10CalendarClient(_BaseHttpClient):
    """金十数据经济日历客户端。"""

    @property
    def name(self) -> str:
        return "jin10"

    def supports_release_watch(self) -> bool:
        return True

    def is_configured(self) -> bool:
        return bool(getattr(self.settings, "jin10_enabled", False))

    def _request_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://rili.jin10.com/",
            "x-app-id": "sKKYe29sFuJaeOCJ",
            "x-version": "2.0",
        }
        token = getattr(self.settings, "jin10_token", None)
        if token:
            headers["x-token"] = token
        return headers

    def _fetch_day(self, day: date, endpoint: str) -> List[Dict[str, Any]]:
        """获取单日数据（/get/data 或 /get/event）。"""
        url = f"{_BASE_URL}{endpoint}"
        response = self._request_json(url, {
            "date": day.strftime("%Y-%m-%d"),
            "category": "cj",
        })
        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, list):
                return data
            logger.debug("Jin10 %s unexpected data type for %s: %s", endpoint, day, type(data).__name__)
            return []
        if isinstance(response, list):
            return response
        return []

    def fetch_events(
        self,
        start_date: date,
        end_date: date,
        countries: Optional[List[str]] = None,
    ) -> List[EconomicCalendarEvent]:
        if not self.is_configured():
            return []

        country_filter = (
            {c.strip().lower() for c in countries} if countries else None
        )

        events: List[EconomicCalendarEvent] = []
        current = start_date
        while current <= end_date:
            try:
                items = self._fetch_day(current, "/get/data")
                events.extend(
                    self._normalize_indicator(item, country_filter)
                    for item in items
                    if self._should_include(item, country_filter, start_date, end_date)
                )
            except EconomicCalendarError:
                logger.warning("Jin10 /get/data failed for %s", current)
            except Exception:
                logger.exception("Jin10 /get/data unexpected error for %s", current)

            try:
                items = self._fetch_day(current, "/get/event")
                events.extend(
                    self._normalize_event(item, country_filter, current)
                    for item in items
                    if self._should_include_event(item, country_filter, start_date, end_date, current)
                )
            except EconomicCalendarError:
                logger.warning("Jin10 /get/event failed for %s", current)
            except Exception:
                logger.exception("Jin10 /get/event unexpected error for %s", current)

            current += timedelta(days=1)

        return events

    def _should_include(
        self,
        item: Dict[str, Any],
        country_filter: Optional[set[str]],
        start_date: date,
        end_date: date,
    ) -> bool:
        pub_time = item.get("pub_time")
        if not pub_time:
            return False
        try:
            scheduled_at = _parse_beijing_time(pub_time)
        except ValueError:
            return False
        if not _event_in_date_range(scheduled_at, start_date, end_date):
            return False
        if country_filter:
            country_cn = _as_text(item.get("country")) or ""
            country_en = _COUNTRY_MAP.get(country_cn, country_cn)
            if country_en.lower() not in country_filter:
                return False
        return True

    def _should_include_event(
        self,
        item: Dict[str, Any],
        country_filter: Optional[set[str]],
        start_date: date,
        end_date: date,
        query_date: date,
    ) -> bool:
        pub_time = item.get("pub_time")
        if pub_time:
            try:
                scheduled_at = _parse_beijing_time(pub_time)
            except ValueError:
                return False
            if not _event_in_date_range(scheduled_at, start_date, end_date):
                return False
        # pub_time 为空 = 全天事件，归属于当前查询日期
        if country_filter:
            country_cn = _as_text(item.get("country")) or ""
            country_en = _COUNTRY_MAP.get(country_cn, country_cn)
            if country_en.lower() not in country_filter:
                return False
        return True

    def _normalize_indicator(
        self,
        item: Dict[str, Any],
        country_filter: Optional[set[str]],
    ) -> EconomicCalendarEvent:
        """将金十经济指标数据转换为 EconomicCalendarEvent。"""
        pub_time = item["pub_time"]
        scheduled_at = _parse_beijing_time(pub_time)

        data_id = item.get("data_id") or item.get("indicator_id")
        if not data_id:
            # fallback: 用指标名+时间生成唯一 ID，避免多条同 pub_time 碰撞
            fallback_name = item.get("indicator_name", "unknown")
            data_id = f"{fallback_name}:{pub_time}"
            logger.debug("Jin10 indicator missing data_id, using fallback: %s", data_id)
        indicator_id = item.get("indicator_id", "")
        provider_event_id = str(data_id)
        event_uid = f"jin10:{provider_event_id}"

        country_cn = _as_text(item.get("country")) or ""
        country_en = _COUNTRY_MAP.get(country_cn, country_cn)
        currency = _COUNTRY_CURRENCY.get(country_cn)

        star = item.get("star") or 0
        importance = _STAR_TO_IMPORTANCE.get(star, 1)

        actual = _as_text(item.get("actual"))
        consensus = _as_text(item.get("consensus"))
        previous = _as_text(item.get("previous"))
        revised = _as_text(item.get("revised"))

        return EconomicCalendarEvent(
            scheduled_at=scheduled_at,
            event_uid=event_uid,
            source="jin10",
            provider_event_id=provider_event_id,
            event_name=_as_text(item.get("indicator_name")) or "unknown",
            country=country_en,
            category="economic_indicator",
            currency=currency,
            reference=_as_text(item.get("time_period")),
            actual=actual,
            previous=previous,
            forecast=consensus,
            revised=revised,
            importance=importance,
            unit=_as_text(item.get("unit")),
            release_id=str(indicator_id) if indicator_id else None,
            all_day=False,
            raw_payload=item,
        )

    def _normalize_event(
        self,
        item: Dict[str, Any],
        country_filter: Optional[set[str]],
        query_date: Optional[date] = None,
    ) -> EconomicCalendarEvent:
        """将金十重大事件转换为 EconomicCalendarEvent。"""
        pub_time = item.get("pub_time", "")
        if pub_time:
            scheduled_at = _parse_beijing_time(pub_time)
        else:
            # 全天事件：归属查询日期的 00:00 UTC
            day = query_date or datetime.now(timezone.utc).date()
            scheduled_at = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)

        event_id = item.get("id") or item.get("event_id") or pub_time
        provider_event_id = f"event:{event_id}"
        event_uid = f"jin10:{provider_event_id}"

        country_cn = _as_text(item.get("country")) or ""
        country_en = _COUNTRY_MAP.get(country_cn, country_cn)
        currency = _COUNTRY_CURRENCY.get(country_cn)

        event_name = (
            _as_text(item.get("event_name"))
            or _as_text(item.get("name"))
            or _as_text(item.get("event_content"))
            or _as_text(item.get("content"))
            or "unknown_event"
        )

        star = item.get("star") or item.get("importance") or 2
        importance = _STAR_TO_IMPORTANCE.get(star, 2)

        is_all_day = not bool(pub_time)

        return EconomicCalendarEvent(
            scheduled_at=scheduled_at,
            event_uid=event_uid,
            source="jin10",
            provider_event_id=provider_event_id,
            event_name=event_name,
            country=country_en,
            category="major_event",
            currency=currency,
            importance=importance,
            all_day=is_all_day,
            raw_payload=item,
        )
