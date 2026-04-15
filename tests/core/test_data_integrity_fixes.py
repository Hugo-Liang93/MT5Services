from __future__ import annotations

import queue
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from src.clients.mt5_market import MT5MarketClient
from src.clients.mt5_market import Tick
from src.clients.base import MT5BaseClient
from src.clients.mt5_trading import MT5TradingClient
from src.market import MarketDataService
from src.persistence.validator import DataValidator
from src.utils.event_store import ClaimedEvent, LocalEventStore


def test_event_store_marks_event_permanently_failed_after_three_retries(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    event_store = LocalEventStore(str(db_path))
    bar_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    event_id = event_store.publish_event("XAUUSD", "M1", bar_time)

    for _ in range(3):
        assert event_store.mark_event_failed_by_id(event_id, "boom")

    with sqlite3.connect(db_path) as conn:
        processed, retry_count = conn.execute(
            "SELECT processed, retry_count FROM ohlc_events WHERE symbol=? AND timeframe=? AND bar_time=?",
            ("XAUUSD", "M1", bar_time.isoformat()),
        ).fetchone()

    assert processed == 3
    assert retry_count == 3


def test_event_store_tracks_skipped_outcomes(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    event_store = LocalEventStore(str(db_path))
    bar_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    event_id = event_store.publish_event("XAUUSD", "M1", bar_time)
    assert event_store.claim_next_event() == ClaimedEvent(event_id=event_id, symbol="XAUUSD", timeframe="M1", bar_time=bar_time)
    assert event_store.mark_event_skipped_by_id(event_id, "insufficient_history")

    stats = event_store.get_stats()

    assert stats["completed"] == 0
    assert stats["skipped"] == 1
    assert stats["retrying"] == 0
    assert stats["outcome_counts"]["skipped_insufficient_history"] == 1
    assert stats["recent_skips"][0]["outcome"] == "skipped_insufficient_history"
    assert stats["recent_errors"] == []
    assert stats["recent_retryable_errors"] == []


def test_event_store_supports_claim_and_completion_by_event_id(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    event_store = LocalEventStore(str(db_path))
    bar_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    event_id = event_store.publish_event("XAUUSD", "M1", bar_time)
    claimed = event_store.claim_next_event()

    assert claimed == ClaimedEvent(event_id=event_id, symbol="XAUUSD", timeframe="M1", bar_time=bar_time)
    assert event_store.mark_event_completed_by_id(event_id)

    with sqlite3.connect(db_path) as conn:
        processed, outcome = conn.execute(
            "SELECT processed, outcome FROM ohlc_events WHERE id=?",
            (event_id,),
        ).fetchone()

    assert processed == 2
    assert outcome == "completed"


def test_event_store_batch_publish_deduplicates_and_tracks_stats(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    event_store = LocalEventStore(str(db_path))
    first_bar_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    second_bar_time = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)

    inserted = event_store.publish_events_batch(
        [
            ("XAUUSD", "M1", first_bar_time),
            ("XAUUSD", "M1", first_bar_time),
            ("XAUUSD", "M1", second_bar_time),
        ]
    )

    assert inserted == 2
    stats = event_store.get_stats()
    assert stats["total"] == 2
    assert stats["pending"] == 2

    duplicate_id = event_store.publish_event("XAUUSD", "M1", first_bar_time)
    claimed = event_store.claim_next_events(limit=2)

    assert duplicate_id == claimed[0].event_id
    assert {event.bar_time for event in claimed} == {first_bar_time, second_bar_time}


def test_event_store_cleanup_keeps_claimed_inflight_events(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    event_store = LocalEventStore(str(db_path))
    old_bar_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    event_id = event_store.publish_event("XAUUSD", "H1", old_bar_time)
    claimed = event_store.claim_next_event()
    assert claimed is not None

    event_store.cleanup_old_events(days_to_keep=7)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, processed FROM ohlc_events WHERE id=?",
            (event_id,),
        ).fetchone()

    assert row == (event_id, 1)


def test_event_store_separates_retrying_and_permanent_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    event_store = LocalEventStore(str(db_path))
    retrying_bar_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    failed_bar_time = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)

    retrying_event_id = event_store.publish_event("XAUUSD", "M1", retrying_bar_time)
    assert event_store.mark_event_failed_by_id(retrying_event_id, "retry me")

    failed_event_id = event_store.publish_event("XAUUSD", "M1", failed_bar_time)
    for _ in range(3):
        assert event_store.mark_event_failed_by_id(failed_event_id, "give up")

    stats = event_store.get_stats()

    assert stats["retrying"] == 1
    assert stats["failed"] == 1
    assert stats["recent_retryable_errors"][0]["error_message"] == "retry me"
    assert stats["recent_errors"][0]["error_message"] == "give up"


def test_mt5_market_client_prefers_millisecond_tick_timestamp() -> None:
    client = object.__new__(MT5MarketClient)
    client._get_field = lambda obj, name, default=None: getattr(obj, name, default)
    client._to_tz = lambda dt: dt
    client._market_time_offset_seconds = None
    client.settings = SimpleNamespace(server_time_offset_hours=None)

    tick = SimpleNamespace(time=1704067200, time_msc=1704067200123)

    ts = client._tick_timestamp(tick)

    assert ts == datetime.fromtimestamp(1704067200.123, tz=timezone.utc)
    assert ts.microsecond == 123000


def test_mt5_market_client_applies_configured_server_time_offset() -> None:
    client = object.__new__(MT5MarketClient)
    client.settings = SimpleNamespace(server_time_offset_hours=3)
    client.tz = ZoneInfo("UTC")
    client._market_time_offset_seconds = 3 * 3600

    normalized = client._market_time_from_seconds(1773686137)

    assert normalized == datetime(2026, 3, 16, 15, 35, 37, tzinfo=timezone.utc)


def test_market_data_service_refreshes_quote_from_mt5_when_cache_is_stale() -> None:
    fresh_quote = SimpleNamespace(
        symbol="XAUUSD",
        bid=3000.0,
        ask=3000.4,
        last=3000.2,
        volume=1.0,
        time=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    stale_quote = SimpleNamespace(
        symbol="XAUUSD",
        bid=2990.0,
        ask=2990.4,
        last=2990.2,
        volume=1.0,
        time=datetime(2025, 12, 31, 23, 59, 55, tzinfo=timezone.utc),
    )
    client = SimpleNamespace(get_quote=lambda symbol: fresh_quote)
    service = MarketDataService(client=client, market_settings=SimpleNamespace(
        default_symbol="XAUUSD",
        quote_stale_seconds=1.0,
        intrabar_max_points=100,
        ohlc_event_queue_size=16,
        tick_limit=200,
        ohlc_limit=200,
        tick_cache_size=100,
    ))
    service.set_quote("XAUUSD", stale_quote)
    service._utc_now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    service._as_utc = lambda value: value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    quote = service.get_quote("XAUUSD")

    assert quote is fresh_quote
    assert service.get_quote("XAUUSD") is fresh_quote


def test_mt5_market_client_fetches_forward_ohlc_window_in_utc() -> None:
    client = object.__new__(MT5MarketClient)
    client.settings = SimpleNamespace(server_time_offset_hours=3)
    client._market_time_offset_seconds = 3 * 3600
    client._configured_market_time_offset_seconds = 3 * 3600
    client.tz = timezone.utc
    client.metrics = SimpleNamespace(record=lambda *args, **kwargs: None)
    client.connect = lambda: None
    client._timeframe_to_mt5 = lambda timeframe: timeframe

    captured = {}
    raw_rates = [
        {
            "time": datetime(2026, 3, 16, 12, 45, tzinfo=timezone.utc).timestamp(),
            "open": 3000.0,
            "high": 3001.0,
            "low": 2999.0,
            "close": 3000.5,
            "real_volume": 1.0,
        },
        {
            "time": datetime(2026, 3, 16, 12, 46, tzinfo=timezone.utc).timestamp(),
            "open": 3000.5,
            "high": 3001.5,
            "low": 3000.0,
            "close": 3001.0,
            "real_volume": 2.0,
        },
    ]
    mock_mt5 = MagicMock()
    mock_mt5.copy_rates_range.side_effect = lambda symbol, tf, start, end: captured.update(
        {"symbol": symbol, "tf": tf, "start": start, "end": end}
    ) or raw_rates

    with patch("src.clients.mt5_market.mt5", mock_mt5):
        bars = client.get_ohlc_from(
            "XAUUSD",
            "M1",
            datetime(2026, 3, 16, 12, 45, tzinfo=timezone.utc),
            1,
        )

    # _market_time_to_request adds the server offset (+3h) to convert UTC → server time
    assert captured["start"] == datetime(2026, 3, 16, 15, 45, tzinfo=timezone.utc)
    assert captured["end"] == datetime(2026, 3, 16, 15, 46, tzinfo=timezone.utc)
    assert len(bars) == 1
    # _market_time_from_seconds normalizes server epoch back to UTC (subtracts 3h)
    assert bars[0].time == datetime(2026, 3, 16, 9, 45, tzinfo=timezone.utc)


def test_mt5_base_client_shutdown_resets_inferred_market_time_offset() -> None:
    client = object.__new__(MT5BaseClient)
    client._connected = True
    client._configured_market_time_offset_seconds = None
    client._market_time_offset_seconds = 3 * 3600

    with patch("src.clients.base.mt5", SimpleNamespace(shutdown=lambda: None)):
        client.shutdown()

    assert client._connected is False
    assert client._market_time_offset_seconds is None


def test_mt5_base_client_initializes_session_with_credentials() -> None:
    """initialize 必须一次性带 path + login + password + server + timeout。

    设计原则：mt5.initialize() 是 MT5 库的"完整 session 建立"接口，
    传入完整凭据后会自动完成"拉起 terminal + 自动登录 + 建立 IPC"，
    避免"terminal 拉起来停在登录界面等人工"的中间不一致状态。
    initialize 已带正确凭据时，后续 mt5.login() 单独调用应不再触发。
    """
    captured = {}

    class FakeMT5:
        def __init__(self) -> None:
            self._initialized = False
            self._logged_in = False

        def initialize(self, **kwargs):
            captured["initialize"] = kwargs
            self._initialized = True
            # 模拟 MT5 库真实行为：initialize 带凭据 → 自动登录到该账户
            if "login" in kwargs and "password" in kwargs and "server" in kwargs:
                self._logged_in = True
            return True

        def terminal_info(self):
            if not self._initialized:
                return None
            return SimpleNamespace(name="TradeMax Global MT5 Terminal")

        def account_info(self):
            if self._logged_in:
                return SimpleNamespace(login=60067107, server="TradeMaxGlobal-Demo")
            return SimpleNamespace(login=12345678, server="Wrong-Server")

        def login(self, **kwargs):
            captured["login"] = kwargs
            self._logged_in = True
            return True

        def last_error(self):
            return (0, "ok")

    client = MT5BaseClient(
        settings=SimpleNamespace(
            timezone="UTC",
            mt5_path="C:/Program Files/TradeMax Global MT5 Terminal/terminal64.exe",
            mt5_login=60067107,
            mt5_password="secret",
            mt5_server="TradeMaxGlobal-Demo",
            server_time_offset_hours=None,
        )
    )

    with patch("src.clients.base.mt5", FakeMT5()):
        with patch.object(MT5BaseClient, "_terminal_path_exists", return_value=True):
            with patch.object(MT5BaseClient, "_terminal_process_running", return_value=True):
                client.connect()

    # initialize 必须包含完整凭据（path + login + password + server + timeout）
    assert captured["initialize"] == {
        "path": "C:/Program Files/TradeMax Global MT5 Terminal/terminal64.exe",
        "login": 60067107,
        "password": "secret",
        "server": "TradeMaxGlobal-Demo",
        "timeout": 60000,
    }
    # initialize 时已带正确凭据，account_match 通过 → mt5.login() 不应被调用
    assert "login" not in captured


def test_mt5_base_client_reuses_matching_existing_session_without_reinitialize() -> None:
    calls = {"initialize": 0, "login": 0}

    class FakeMT5:
        def initialize(self, **kwargs):
            calls["initialize"] += 1
            return True

        def terminal_info(self):
            return SimpleNamespace(name="TradeMax Global MT5 Terminal")

        def account_info(self):
            return SimpleNamespace(login=60067107, server="TradeMaxGlobal-Demo")

        def login(self, **kwargs):
            calls["login"] += 1
            return True

        def last_error(self):
            return (0, "ok")

    client = MT5BaseClient(
        settings=SimpleNamespace(
            timezone="UTC",
            mt5_path="C:/Program Files/TradeMax Global MT5 Terminal/terminal64.exe",
            mt5_login=60067107,
            mt5_password="secret",
            mt5_server="TradeMaxGlobal-Demo",
            server_time_offset_hours=None,
        )
    )

    with patch("src.clients.base.mt5", FakeMT5()):
        client.connect()

    assert calls["initialize"] == 0
    assert calls["login"] == 0
    assert client._connected is True


def test_mt5_base_client_classifies_interactive_login_required() -> None:
    client = MT5BaseClient(
        settings=SimpleNamespace(
            timezone="UTC",
            mt5_path="C:/Program Files/TradeMax Global MT5 Terminal/terminal64.exe",
            mt5_login=60067107,
            mt5_password="secret",
            mt5_server="TradeMaxGlobal-Demo",
            server_time_offset_hours=None,
        )
    )

    class FakeMT5:
        def initialize(self, **kwargs):
            return False

        def terminal_info(self):
            return None

        def account_info(self):
            return None

        def last_error(self):
            return (-10005, "IPC timeout")

    with patch("src.clients.base.mt5", FakeMT5()):
        with patch.object(MT5BaseClient, "_terminal_path_exists", return_value=True):
            with patch.object(MT5BaseClient, "_terminal_process_running", return_value=True):
                state = client.inspect_session_state()

    assert state.session_ready is False
    assert state.interactive_login_required is True
    assert state.error_code == "interactive_login_required"


def test_mt5_base_client_blocks_when_terminal_process_not_running() -> None:
    client = MT5BaseClient(
        settings=SimpleNamespace(
            timezone="UTC",
            mt5_path="C:/Program Files/TradeMax Global MT5 Terminal/terminal64.exe",
            mt5_login=60067107,
            mt5_password="secret",
            mt5_server="TradeMaxGlobal-Demo",
            server_time_offset_hours=None,
        )
    )

    class FakeMT5:
        def terminal_info(self):
            return None

        def account_info(self):
            return None

        def last_error(self):
            return (0, "ok")

    with patch("src.clients.base.mt5", FakeMT5()):
        with patch.object(MT5BaseClient, "_terminal_path_exists", return_value=True):
            with patch.object(MT5BaseClient, "_terminal_process_running", return_value=False):
                state = client.inspect_session_state()

    assert state.session_ready is False
    assert state.error_code == "terminal_not_running"


def test_mt5_market_client_uses_default_tick_lookback_without_ingest_settings() -> None:
    client = object.__new__(MT5MarketClient)
    client.settings = SimpleNamespace()
    client.metrics = SimpleNamespace(record=lambda *args, **kwargs: None)
    client.connect = lambda: None
    client._extract_price = lambda tick: 100.0
    client._extract_volume = lambda tick: 1.0
    client._tick_timestamp = lambda tick: datetime(2026, 1, 1, tzinfo=timezone.utc)
    client._tick_time_msc = lambda tick: 1767225600000

    captured = {}

    mock_mt5 = MagicMock()
    mock_mt5.COPY_TICKS_ALL = 1
    mock_mt5.copy_ticks_from.side_effect = lambda symbol, start, limit, flags: captured.update(
        {"symbol": symbol, "start": start, "limit": limit, "flags": flags}
    ) or [SimpleNamespace()]

    with patch("src.clients.mt5_market.mt5", mock_mt5):
        ticks = client.get_ticks("XAUUSD", 10, None)

    assert len(ticks) == 1
    assert captured["symbol"] == "XAUUSD"
    assert captured["limit"] == 10
    assert isinstance(captured["start"], datetime)


def test_mt5_market_client_normalizes_quote_last_price_from_bid_ask() -> None:
    client = object.__new__(MT5MarketClient)
    client.metrics = SimpleNamespace(record=lambda *args, **kwargs: None)
    client.connect = lambda: None
    client._get_field = lambda obj, name, default=None: getattr(obj, name, default)
    client._market_time_from_seconds = lambda value: datetime(2026, 1, 1, tzinfo=timezone.utc)

    mock_mt5 = MagicMock()
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(
        bid=5000.0,
        ask=5000.4,
        last=0.0,
        volume=12.0,
        time=1767225600,
    )

    with patch("src.clients.mt5_market.mt5", mock_mt5):
        quote = client.get_quote("XAUUSD")

    assert quote.last == 5000.2


def test_mt5_trading_client_estimate_margin_uses_public_order_type_mapper() -> None:
    client = object.__new__(MT5TradingClient)
    client.connect = lambda: None
    client._validate_volume = lambda symbol, volume: None

    captured = {}

    def _order_type(side: str, order_kind: str = "market") -> int:
        captured["side"] = side
        captured["order_kind"] = order_kind
        return 0

    client.side_and_kind_to_order_type = _order_type

    mock_mt5 = MagicMock()
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(ask=3025.5, bid=3025.2)
    mock_mt5.order_calc_margin.return_value = 123.45
    mock_mt5.ORDER_TYPE_BUY = 0

    with patch("src.clients.mt5_trading.mt5", mock_mt5):
        margin = client.estimate_margin("XAUUSD", 0.01, "buy")

    assert margin == 123.45
    assert captured == {"side": "buy", "order_kind": "market"}


def test_mt5_trading_client_retries_open_with_supported_filling_mode() -> None:
    client = object.__new__(MT5TradingClient)
    client.connect = lambda: None
    client._validate_volume = lambda symbol, volume: None
    client._validate_protection_levels = lambda **kwargs: None

    requests = []

    class FakeMT5:
        ORDER_TYPE_BUY = 0
        ORDER_FILLING_FOK = 0
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_RETURN = 2
        TRADE_ACTION_DEAL = 1
        TRADE_ACTION_PENDING = 5
        TRADE_RETCODE_DONE = 10009

        @staticmethod
        def symbol_info_tick(symbol):
            return SimpleNamespace(ask=3025.6, bid=3025.3)

        @staticmethod
        def symbol_info(symbol):
            return SimpleNamespace(filling_mode=2)

        @staticmethod
        def order_send(request):
            requests.append(dict(request))
            if request["type_filling"] != 2:
                return SimpleNamespace(retcode=10030, comment="Unsupported filling mode")
            return SimpleNamespace(retcode=10009, comment="Done", order=123456, deal=123456)

        @staticmethod
        def last_error():
            return (0, "ok")

    with patch("src.clients.mt5_trading.mt5", FakeMT5):
        result = client.open_trade_details(
            symbol="XAUUSD",
            volume=0.01,
            order_type=FakeMT5.ORDER_TYPE_BUY,
            sl=3015.6,
            tp=3045.6,
        )

    assert result.ticket == 123456
    assert requests[-1]["type_filling"] == 2


def test_mt5_trading_client_retries_close_with_supported_filling_mode() -> None:
    client = object.__new__(MT5TradingClient)
    client.connect = lambda: None

    requests = []

    class FakeMT5:
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_FILLING_FOK = 0
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_RETURN = 2
        TRADE_ACTION_DEAL = 1
        TRADE_RETCODE_DONE = 10009

        @staticmethod
        def positions_get(ticket=None):
            return [
                SimpleNamespace(
                    ticket=777,
                    symbol="XAUUSD",
                    volume=0.01,
                    type=0,
                    magic=0,
                )
            ]

        @staticmethod
        def symbol_info_tick(symbol):
            return SimpleNamespace(ask=3025.6, bid=3025.3)

        @staticmethod
        def symbol_info(symbol):
            return SimpleNamespace(filling_mode=2)

        @staticmethod
        def order_send(request):
            requests.append(dict(request))
            if request["type_filling"] != 2:
                return SimpleNamespace(retcode=10030, comment="Unsupported filling mode")
            return SimpleNamespace(retcode=10009, comment="Done")

        @staticmethod
        def last_error():
            return (0, "ok")

    with patch("src.clients.mt5_trading.mt5", FakeMT5):
        ok = client.close_position(ticket=777, deviation=50, comment="close")

    assert ok is True
    assert requests[-1]["type_filling"] == 2


def test_mt5_trading_client_retries_close_when_order_send_returns_none() -> None:
    client = object.__new__(MT5TradingClient)
    client.connect = lambda: None

    send_calls = {"count": 0}

    class FakeMT5:
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_FILLING_FOK = 0
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_RETURN = 2
        TRADE_ACTION_DEAL = 1
        TRADE_RETCODE_DONE = 10009

        @staticmethod
        def positions_get(ticket=None):
            return [
                SimpleNamespace(
                    ticket=777,
                    symbol="XAUUSD",
                    volume=0.01,
                    type=0,
                    magic=0,
                )
            ]

        @staticmethod
        def symbol_info_tick(symbol):
            return SimpleNamespace(ask=3025.6, bid=3025.3)

        @staticmethod
        def symbol_info(symbol):
            return SimpleNamespace(filling_mode=2)

        @staticmethod
        def order_send(request):
            send_calls["count"] += 1
            if send_calls["count"] == 1:
                return None
            return SimpleNamespace(retcode=10009, comment="Done")

        @staticmethod
        def last_error():
            return (10004, "trade context busy")

    with patch("src.clients.mt5_trading.mt5", FakeMT5):
        ok = client.close_position(ticket=777, deviation=50, comment="close")

    assert ok is True
    assert send_calls["count"] == 2


def test_mt5_trading_client_normalizes_mt5_comment() -> None:
    client = object.__new__(MT5TradingClient)

    normalized = client._normalize_comment("codex_runtime_close:dispatch_api", "close")

    assert normalized == "codex_runtime_close_dispatc"
    assert len(normalized) <= 27


def test_quote_validator_allows_missing_last_trade_price() -> None:
    valid, message = DataValidator.validate_quote(
        "XAUUSD",
        5000.0,
        5000.4,
        0.0,
        0.0,
        "2026-01-01T00:00:00+00:00",
    )

    assert valid is True
    assert message == ""


def test_storage_writer_block_policy_waits_for_capacity_instead_of_dropping() -> None:
    psycopg2_stub = SimpleNamespace(
        OperationalError=RuntimeError,
        Error=RuntimeError,
    )
    extras_stub = SimpleNamespace(
        Json=lambda value: value,
        execute_batch=lambda *args, **kwargs: None,
        execute_values=lambda *args, **kwargs: None,
    )
    pool_stub = SimpleNamespace(SimpleConnectionPool=object)

    with patch.dict(
        sys.modules,
        {
            "psycopg2": psycopg2_stub,
            "psycopg2.extras": extras_stub,
            "psycopg2.pool": pool_stub,
        },
    ):
        from src.persistence.storage_writer import StorageWriter

    writer = object.__new__(StorageWriter)
    writer.settings = SimpleNamespace(queue_put_timeout=0.05)
    writer._stop = threading.Event()
    writer._thread = SimpleNamespace(is_alive=lambda: True)
    writer._channel_stats = {
        "ohlc": {
            "dropped_oldest": 0,
            "dropped_newest": 0,
            "blocked_puts": 0,
            "full_errors": 0,
        }
    }

    q: queue.Queue = queue.Queue(maxsize=1)
    q.put(("existing",))
    channel = {"queue": q, "type": "ohlc", "overflow_policy": "block"}

    def consumer() -> None:
        time.sleep(0.06)
        q.get_nowait()

    thread = threading.Thread(target=consumer)
    thread.start()
    writer._handle_queue_full("ohlc", channel, ("new",))
    thread.join(timeout=1.0)

    assert writer._channel_stats["ohlc"]["blocked_puts"] == 1
    assert writer._channel_stats["ohlc"]["dropped_newest"] == 0
    assert q.get_nowait() == ("new",)


def test_timescale_writer_normalizes_tick_rows_with_time_msc() -> None:
    psycopg2_stub = SimpleNamespace(
        OperationalError=RuntimeError,
        Error=RuntimeError,
    )
    extras_stub = SimpleNamespace(
        Json=lambda value: value,
        execute_batch=lambda *args, **kwargs: None,
        execute_values=lambda *args, **kwargs: None,
    )
    pool_stub = SimpleNamespace(SimpleConnectionPool=object)

    with patch.dict(
        sys.modules,
        {
            "psycopg2": psycopg2_stub,
            "psycopg2.extras": extras_stub,
            "psycopg2.pool": pool_stub,
        },
    ):
        from src.persistence.db import TimescaleWriter

    writer = object.__new__(TimescaleWriter)
    captured = []
    writer._batch = lambda sql, rows, page_size=1000: captured.extend(rows)

    writer.write_ticks([("XAUUSD", 100.0, 1.0, "2026-01-01T00:00:00.123000+00:00")])

    assert captured == [("XAUUSD", 100.0, 1.0, "2026-01-01T00:00:00.123000+00:00", 1767225600123)]


def test_storage_writer_stats_include_queue_summary() -> None:
    from src.persistence.storage_writer import StorageWriter

    writer = object.__new__(StorageWriter)
    writer._lock = threading.RLock()
    writer._thread = SimpleNamespace(is_alive=lambda: True)
    writer._channels = {
        "ohlc": {
            "queue": queue.Queue(maxsize=10),
            "pending": deque([("pending",)] * 2),
            "overflow_policy": "block",
        }
    }
    writer._channel_stats = {
        "ohlc": {
            "dropped_oldest": 0,
            "dropped_newest": 0,
            "blocked_puts": 1,
            "full_errors": 0,
        }
    }

    for i in range(9):
        writer._channels["ohlc"]["queue"].put_nowait((i,))  # type: ignore[index]

    stats = writer.stats()

    assert stats["summary"]["high"] == 1
    assert stats["queues"]["ohlc"]["status"] == "high"
    assert stats["queues"]["ohlc"]["utilization_pct"] == 90.0


def test_timescale_writer_wraps_ohlc_indicators_with_json_adapter() -> None:
    json_calls = []
    from src.persistence.db import TimescaleWriter
    import src.persistence.db as persistence_db

    writer = object.__new__(TimescaleWriter)
    captured = []
    writer._batch = lambda sql, rows, page_size=1000: captured.extend(rows)

    with patch.object(
        persistence_db,
        "Json",
        side_effect=lambda value: json_calls.append(value) or ("json", value),
    ):
        writer.write_ohlc(
            [
                (
                    "XAUUSD",
                    "M1",
                    100.0,
                    101.0,
                    99.0,
                    100.5,
                    1.0,
                    "2026-01-01T00:00:00+00:00",
                    {"macd": {"value": 1.2}},
                )
            ],
            upsert=True,
        )

    assert json_calls == [{"macd": {"value": 1.2}}]
    assert captured[0][8] == ("json", {"macd": {"value": 1.2}})


def test_tick_schema_includes_time_msc_backfill_migration() -> None:
    from src.persistence.schema.ticks import DDL

    # time_msc 已合并到主 DDL（不再是 ALTER TABLE migration）
    assert "time_msc" in DDL
    assert "idx_ticks_symbol_time_msc" in DDL


def test_market_service_merge_ticks_orders_by_time_msc() -> None:
    tick_late = Tick(
        symbol="XAUUSD",
        price=1.0,
        volume=1.0,
        time=datetime(2026, 1, 1, 0, 0, 0, 200000, tzinfo=timezone.utc),
        time_msc=1767225600200,
    )
    tick_early = Tick(
        symbol="XAUUSD",
        price=1.0,
        volume=1.0,
        time=datetime(2026, 1, 1, 0, 0, 0, 100000, tzinfo=timezone.utc),
        time_msc=1767225600100,
    )

    merged = MarketDataService._merge_ticks([tick_late], [tick_early])

    assert merged == [tick_early, tick_late]


def test_runtime_ingest_settings_exposes_intrabar_enabled() -> None:
    from src.config import get_runtime_ingest_settings

    get_runtime_ingest_settings.cache_clear()
    settings = get_runtime_ingest_settings()

    assert hasattr(settings, "intrabar_enabled")


def test_market_service_removes_bound_method_listeners_by_callable_identity() -> None:
    service = MarketDataService(client=SimpleNamespace())

    class ListenerOwner:
        def __init__(self) -> None:
            self.closed_calls = 0
            self.intrabar_calls = 0

        def on_close(self, _symbol: str, _timeframe: str, _bar_time: datetime) -> None:
            self.closed_calls += 1

        def on_intrabar(self, _symbol: str, _timeframe: str, _bar: Tick) -> None:
            self.intrabar_calls += 1

    owner = ListenerOwner()

    service.add_ohlc_close_listener(owner.on_close)
    service.add_intrabar_listener(owner.on_intrabar)

    service.remove_ohlc_close_listener(owner.on_close)
    service.remove_intrabar_listener(owner.on_intrabar)

    service.enqueue_ohlc_closed_event("XAUUSD", "M1", datetime(2026, 1, 1, tzinfo=timezone.utc))
    service.set_intrabar("XAUUSD", "M1", SimpleNamespace(time=datetime(2026, 1, 1, tzinfo=timezone.utc)))

    assert owner.closed_calls == 0
    assert owner.intrabar_calls == 0


def test_market_service_converts_spread_to_symbol_points() -> None:
    quote = SimpleNamespace(
        symbol="XAUUSD",
        bid=3000.00,
        ask=3000.25,
        time=datetime.now(timezone.utc),
    )
    client = SimpleNamespace(
        get_symbol_info=lambda symbol: SimpleNamespace(symbol=symbol, point=0.01)
    )
    service = MarketDataService(client=client)
    service.set_quote("XAUUSD", quote)

    spread_points = service.get_current_spread("XAUUSD")

    assert spread_points == 25.0
