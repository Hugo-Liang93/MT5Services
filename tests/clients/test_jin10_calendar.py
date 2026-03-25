"""Jin10 (金十数据) 经济日历客户端单元测试。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.clients.economic_calendar import (
    EconomicCalendarEvent,
    EconomicCalendarProvider,
)
from src.clients.jin10_calendar import (
    Jin10CalendarClient,
    _BEIJING_OFFSET,
    _COUNTRY_MAP,
    _STAR_TO_IMPORTANCE,
    _parse_beijing_time,
)


def _make_settings(enabled: bool = True) -> MagicMock:
    settings = MagicMock()
    settings.jin10_enabled = enabled
    settings.request_retries = 1
    settings.request_timeout_seconds = 5.0
    settings.retry_backoff_seconds = 0.1
    settings.refresh_jitter_seconds = 0.0
    return settings


# ────────────────────── Protocol 合规性 ──────────────────────


@pytest.mark.unit
class TestJin10Protocol:
    def test_satisfies_provider_protocol(self):
        client = Jin10CalendarClient(_make_settings())
        assert isinstance(client, EconomicCalendarProvider)

    def test_name(self):
        client = Jin10CalendarClient(_make_settings())
        assert client.name == "jin10"

    def test_supports_release_watch(self):
        client = Jin10CalendarClient(_make_settings())
        assert client.supports_release_watch() is True

    def test_is_configured_when_enabled(self):
        client = Jin10CalendarClient(_make_settings(enabled=True))
        assert client.is_configured() is True

    def test_not_configured_when_disabled(self):
        client = Jin10CalendarClient(_make_settings(enabled=False))
        assert client.is_configured() is False

    def test_fetch_returns_empty_when_disabled(self):
        client = Jin10CalendarClient(_make_settings(enabled=False))
        result = client.fetch_events(date(2026, 3, 25), date(2026, 3, 25))
        assert result == []


# ────────────────────── 时间解析 ──────────────────────


@pytest.mark.unit
class TestBeijingTimeParse:
    def test_parse_minute_format(self):
        dt = _parse_beijing_time("2026-03-25 04:30")
        # 北京时间 04:30 → UTC 前一天 20:30
        assert dt.tzinfo == timezone.utc
        assert dt == datetime(2026, 3, 24, 20, 30, tzinfo=timezone.utc)

    def test_parse_second_format(self):
        dt = _parse_beijing_time("2026-03-25 16:00:00")
        assert dt == datetime(2026, 3, 25, 8, 0, tzinfo=timezone.utc)

    def test_parse_invalid_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_beijing_time("invalid-time")

    def test_midnight_beijing(self):
        dt = _parse_beijing_time("2026-03-25 00:00")
        # 北京时间 00:00 = UTC 前一天 16:00
        assert dt == datetime(2026, 3, 24, 16, 0, tzinfo=timezone.utc)


# ────────────────────── 星级映射 ──────────────────────


@pytest.mark.unit
class TestStarMapping:
    @pytest.mark.parametrize("star,expected", [(1, 1), (2, 1), (3, 2), (4, 3), (5, 3)])
    def test_star_to_importance(self, star: int, expected: int):
        assert _STAR_TO_IMPORTANCE[star] == expected


# ────────────────────── 国家映射 ──────────────────────


@pytest.mark.unit
class TestCountryMap:
    def test_major_countries_mapped(self):
        assert _COUNTRY_MAP["美国"] == "United States"
        assert _COUNTRY_MAP["中国"] == "China"
        assert _COUNTRY_MAP["欧元区"] == "Euro Area"
        assert _COUNTRY_MAP["英国"] == "United Kingdom"
        assert _COUNTRY_MAP["日本"] == "Japan"
        assert _COUNTRY_MAP["瑞士"] == "Switzerland"

    def test_unmapped_country_passes_through(self):
        """未映射的国家名应原样传递。"""
        client = Jin10CalendarClient(_make_settings())
        item = {
            "data_id": 1,
            "indicator_id": 100,
            "pub_time": "2026-03-25 10:00",
            "indicator_name": "Test",
            "country": "未知国家",
            "star": 2,
        }
        event = client._normalize_indicator(item, None)
        assert event.country == "未知国家"


# ────────────────────── 指标数据规范化 ──────────────────────


@pytest.mark.unit
class TestNormalizeIndicator:
    def setup_method(self):
        self.client = Jin10CalendarClient(_make_settings())
        self.sample_item = {
            "data_id": 1158509,
            "indicator_id": 8018,
            "time_period": "至3月24日",
            "previous": "3.926",
            "consensus": "4.0",
            "actual": "3.984",
            "revised": None,
            "pub_time": "2026-03-25 04:30",
            "indicator_name": "2年期国债竞拍-得标利率",
            "country": "美国",
            "star": 2,
            "unit": "%",
            "affect": 1,
            "lock": False,
        }

    def test_event_uid_format(self):
        event = self.client._normalize_indicator(self.sample_item, None)
        assert event.event_uid == "jin10:1158509"

    def test_source(self):
        event = self.client._normalize_indicator(self.sample_item, None)
        assert event.source == "jin10"

    def test_scheduled_at_utc(self):
        event = self.client._normalize_indicator(self.sample_item, None)
        # 北京 04:30 → UTC 前一天 20:30
        assert event.scheduled_at == datetime(2026, 3, 24, 20, 30, tzinfo=timezone.utc)

    def test_country_mapped(self):
        event = self.client._normalize_indicator(self.sample_item, None)
        assert event.country == "United States"

    def test_currency_inferred(self):
        event = self.client._normalize_indicator(self.sample_item, None)
        assert event.currency == "USD"

    def test_importance_from_star(self):
        event = self.client._normalize_indicator(self.sample_item, None)
        assert event.importance == 1  # star=2 → importance=1

    def test_high_star_importance(self):
        item = {**self.sample_item, "star": 5}
        event = self.client._normalize_indicator(item, None)
        assert event.importance == 3

    def test_values_mapped(self):
        event = self.client._normalize_indicator(self.sample_item, None)
        assert event.actual == "3.984"
        assert event.forecast == "4.0"
        assert event.previous == "3.926"

    def test_none_actual_preserved(self):
        item = {**self.sample_item, "actual": None, "consensus": None, "previous": None}
        event = self.client._normalize_indicator(item, None)
        assert event.actual is None
        assert event.forecast is None
        assert event.previous is None

    def test_unit_mapped(self):
        event = self.client._normalize_indicator(self.sample_item, None)
        assert event.unit == "%"

    def test_category(self):
        event = self.client._normalize_indicator(self.sample_item, None)
        assert event.category == "economic_indicator"

    def test_raw_payload_preserved(self):
        event = self.client._normalize_indicator(self.sample_item, None)
        assert event.raw_payload == self.sample_item


# ────────────────────── 事件规范化 ──────────────────────


@pytest.mark.unit
class TestNormalizeEvent:
    def setup_method(self):
        self.client = Jin10CalendarClient(_make_settings())
        self.sample_event = {
            "id": 12345,
            "pub_time": "2026-03-25 22:00",
            "country": "瑞士",
            "event_name": "瑞士央行行长施莱格尔发表讲话",
            "star": 4,
        }

    def test_event_uid_format(self):
        event = self.client._normalize_event(self.sample_event, None)
        assert event.event_uid == "jin10:event:12345"

    def test_country_mapped(self):
        event = self.client._normalize_event(self.sample_event, None)
        assert event.country == "Switzerland"

    def test_currency_inferred(self):
        event = self.client._normalize_event(self.sample_event, None)
        assert event.currency == "CHF"

    def test_importance_from_star(self):
        event = self.client._normalize_event(self.sample_event, None)
        assert event.importance == 3  # star=4 → importance=3

    def test_category(self):
        event = self.client._normalize_event(self.sample_event, None)
        assert event.category == "major_event"

    def test_no_pub_time_is_all_day(self):
        item = {**self.sample_event, "pub_time": ""}
        event = self.client._normalize_event(item, None, date(2026, 3, 25))
        assert event.all_day is True
        assert event.scheduled_at == datetime(2026, 3, 25, tzinfo=timezone.utc)


# ────────────────────── fetch_events 集成 ──────────────────────


@pytest.mark.unit
class TestFetchEvents:
    def setup_method(self):
        self.client = Jin10CalendarClient(_make_settings())

    @patch.object(Jin10CalendarClient, "_request_json")
    def test_single_day_fetch(self, mock_request):
        indicator_item = {
            "data_id": 100,
            "indicator_id": 200,
            "pub_time": "2026-03-25 10:00",
            "indicator_name": "CPI",
            "country": "美国",
            "star": 5,
            "unit": "%",
        }
        event_item = {
            "id": 300,
            "pub_time": "2026-03-25 15:00",
            "country": "美国",
            "event_name": "Fed Speech",
            "star": 3,
        }
        # /get/data 和 /get/event 各调用一次
        mock_request.side_effect = [
            {"status": 200, "data": [indicator_item]},
            {"status": 200, "data": [event_item]},
        ]

        events = self.client.fetch_events(date(2026, 3, 25), date(2026, 3, 25))
        assert len(events) == 2
        assert events[0].event_uid == "jin10:100"
        assert events[1].event_uid == "jin10:event:300"

    @patch.object(Jin10CalendarClient, "_request_json")
    def test_multi_day_fetch(self, mock_request):
        """多天查询应该逐天请求。"""
        mock_request.return_value = {"status": 200, "data": []}
        self.client.fetch_events(date(2026, 3, 24), date(2026, 3, 26))
        # 3 天 × 2 端点 = 6 次调用
        assert mock_request.call_count == 6

    @patch.object(Jin10CalendarClient, "_request_json")
    def test_country_filter(self, mock_request):
        """国家过滤应正确应用。"""
        items = [
            {"data_id": 1, "indicator_id": 1, "pub_time": "2026-03-25 10:00",
             "indicator_name": "US CPI", "country": "美国", "star": 3},
            {"data_id": 2, "indicator_id": 2, "pub_time": "2026-03-25 10:00",
             "indicator_name": "JP CPI", "country": "日本", "star": 3},
        ]
        mock_request.side_effect = [
            {"status": 200, "data": items},
            {"status": 200, "data": []},
        ]

        events = self.client.fetch_events(
            date(2026, 3, 25), date(2026, 3, 25),
            countries=["United States"],
        )
        assert len(events) == 1
        assert events[0].country == "United States"

    @patch.object(Jin10CalendarClient, "_request_json")
    def test_api_error_graceful(self, mock_request):
        """API 错误时不崩溃，返回空列表。"""
        from src.clients.economic_calendar import EconomicCalendarError
        mock_request.side_effect = EconomicCalendarError("timeout")
        events = self.client.fetch_events(date(2026, 3, 25), date(2026, 3, 25))
        assert events == []
