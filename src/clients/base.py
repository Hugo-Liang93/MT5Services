"""
MT5 客户端基类：统一初始化/登录、时区转换、字段提取等通用逻辑。
"""

from __future__ import annotations

import logging
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional
from zoneinfo import ZoneInfo

from src.config import MT5Settings, load_mt5_settings

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None


logger = logging.getLogger(__name__)


class MetricsRecorder:
    """简单耗时/错误计数器，按方法名聚合。"""

    def __init__(self):
        self._stats = {}

    def record(self, name: str, duration_ms: float, error: bool = False):
        stat = self._stats.setdefault(name, {"calls": 0, "errors": 0, "total_ms": 0.0})
        stat["calls"] += 1
        stat["total_ms"] += duration_ms
        if error:
            stat["errors"] += 1

    def snapshot(self) -> dict:
        return {k: v.copy() for k, v in self._stats.items()}


class MT5BaseError(RuntimeError):
    """基础 MT5 异常类型。"""


class MT5TradeError(MT5BaseError):
    """统一的交易/账户 API 错误基类。"""


class MT5BaseClient:
    _session_lock = threading.RLock()

    # 全局"上次成功验证会话"时间戳，避免高并发下每次 connect() 都持锁调用 terminal_info()。
    # 5 秒内已验证的连接直接走快速路径，无需争用锁。
    _last_session_check: float = 0.0
    _SESSION_CHECK_TTL: float = 5.0  # seconds

    def __init__(self, settings: Optional[MT5Settings] = None):
        self.settings = settings or load_mt5_settings()
        self.tz = ZoneInfo(self.settings.timezone)
        self._connected = False
        self.metrics = MetricsRecorder()
        configured_offset_hours = getattr(self.settings, "server_time_offset_hours", None)
        self._configured_market_time_offset_seconds: Optional[int] = (
            int(configured_offset_hours) * 3600 if configured_offset_hours is not None else None
        )
        self._market_time_offset_seconds: Optional[int] = self._configured_market_time_offset_seconds

    def _matches_requested_account(self) -> bool:
        account_info = mt5.account_info()
        if account_info is None:
            return not bool(self.settings.mt5_login)

        current_login = getattr(account_info, "login", None)
        current_server = getattr(account_info, "server", None)
        if self.settings.mt5_login and current_login != self.settings.mt5_login:
            return False
        if self.settings.mt5_server and current_server != self.settings.mt5_server:
            return False
        return True

    def _session_ready(self) -> bool:
        try:
            terminal_info = mt5.terminal_info()
        except Exception:
            return False
        if terminal_info is None:
            return False
        return self._matches_requested_account()

    def _initialize_session(self) -> None:
        initialize_kwargs = {}
        if self.settings.mt5_path:
            initialize_kwargs["path"] = self.settings.mt5_path
        if self.settings.mt5_login is not None:
            initialize_kwargs["login"] = self.settings.mt5_login
        if self.settings.mt5_password:
            initialize_kwargs["password"] = self.settings.mt5_password
        if self.settings.mt5_server:
            initialize_kwargs["server"] = self.settings.mt5_server

        if not mt5.initialize(**initialize_kwargs):
            raise MT5BaseError(f"Failed to initialize MT5: {mt5.last_error()}")

    def connect(self):
        if mt5 is None:
            raise MT5BaseError("MetaTrader5 package is not installed")
        # 快速路径：已连接且近期（5s 内）已验证过会话，直接返回，无需争用类级锁。
        now = time.monotonic()
        if self._connected and (now - MT5BaseClient._last_session_check) < MT5BaseClient._SESSION_CHECK_TTL:
            return
        with self._session_lock:
            self._market_time_offset_seconds = self._configured_market_time_offset_seconds
            if self._connected and self._session_ready():
                MT5BaseClient._last_session_check = time.monotonic()
                return

            if self._session_ready():
                self._connected = True
                return

            self._initialize_session()

            if self.settings.mt5_login and not self._matches_requested_account():
                login_kwargs = {"login": self.settings.mt5_login}
                if self.settings.mt5_password:
                    login_kwargs["password"] = self.settings.mt5_password
                if self.settings.mt5_server:
                    login_kwargs["server"] = self.settings.mt5_server
                authorized = mt5.login(**login_kwargs)
                if not authorized:
                    raise MT5BaseError(f"Failed to login to MT5: {mt5.last_error()}")
            self._connected = True

    def shutdown(self):
        if self._connected and mt5:
            with self._session_lock:
                mt5.shutdown()
        self._connected = False
        self._market_time_offset_seconds = self._configured_market_time_offset_seconds

    def health(self) -> dict:
        """基础健康检查：连接状态、账户/终端信息。"""
        try:
            self.connect()
            terminal_info = mt5.terminal_info()
            account_info = mt5.account_info()
            return {
                "connected": True,
                "login": account_info.login if account_info else None,
                "server": account_info.server if account_info else None,
                "terminal": terminal_info.name if terminal_info else None,
            }
        except Exception as exc:  # pragma: no cover - 防御性
            return {"connected": False, "error": str(exc)}

    def _to_tz(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    def _normalize_market_time(self, raw_utc: datetime) -> datetime:
        offset_seconds = self._resolve_market_time_offset_seconds(raw_utc)
        if not offset_seconds:
            return raw_utc
        return raw_utc - timedelta(seconds=offset_seconds)

    def _resolve_market_time_offset_seconds(self, raw_utc: datetime) -> int:
        if self._market_time_offset_seconds is not None:
            return self._market_time_offset_seconds

        now_utc = datetime.now(timezone.utc)
        skew_seconds = (raw_utc - now_utc).total_seconds()
        inferred_hours = int(round(skew_seconds / 3600.0))
        if inferred_hours == 0 or abs(inferred_hours) > 14:
            return 0
        if abs(skew_seconds - (inferred_hours * 3600)) > 900:
            return 0

        self._market_time_offset_seconds = inferred_hours * 3600
        logger.warning(
            "Detected MT5 market time offset of %+sh from live timestamps; normalizing to UTC",
            inferred_hours,
        )
        return self._market_time_offset_seconds

    def _market_time_from_seconds(self, timestamp_seconds: float) -> datetime:
        raw_utc = datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
        return self._to_tz(self._normalize_market_time(raw_utc))

    def _market_time_from_milliseconds(self, timestamp_msc: int) -> datetime:
        raw_utc = datetime.fromtimestamp(float(timestamp_msc) / 1000.0, tz=timezone.utc)
        return self._to_tz(self._normalize_market_time(raw_utc))

    def _market_time_to_request(self, dt: datetime) -> datetime:
        """Convert a UTC datetime to MT5 server-local time for API requests.

        All ``mt5.copy_*``, ``mt5.history_*`` APIs expect server-local time.
        Use this before passing time parameters to any MT5 API.
        """
        if dt.tzinfo is None:
            utc_dt = dt.replace(tzinfo=timezone.utc)
        else:
            utc_dt = dt.astimezone(timezone.utc)
        offset_seconds = self._market_time_offset_seconds or 0
        if not offset_seconds:
            return utc_dt
        return utc_dt + timedelta(seconds=offset_seconds)

    def _server_now(self) -> datetime:
        """Return ``datetime.now()`` in MT5 server time, for use with MT5 APIs."""
        return self._market_time_to_request(datetime.now(timezone.utc))

    def _request_time_range(
        self, start_utc: datetime, end_utc: datetime,
    ) -> tuple[datetime, datetime]:
        """Convert a UTC time range to server time for MT5 API requests."""
        return self._market_time_to_request(start_utc), self._market_time_to_request(end_utc)

    def _parse_server_timestamp(self, epoch_seconds: float) -> datetime:
        """Convert an MT5 server-time epoch to a normalized UTC datetime.

        MT5 APIs return timestamps as epoch seconds in server-local time.
        This method applies the detected offset to normalize back to UTC.
        Use this for all ``rate['time']``, ``deal.time``, ``order.time_setup`` etc.
        """
        return self._market_time_from_seconds(epoch_seconds)

    def _parse_server_timestamp_msc(self, epoch_msc: int) -> datetime:
        """Convert an MT5 server-time millisecond epoch to normalized UTC."""
        return self._market_time_from_milliseconds(epoch_msc)

    def _get_field(self, obj, name: str, default=None):
        """兼容 numpy.void/dict/对象字段，返回基础类型值。"""
        if hasattr(obj, name):
            val = getattr(obj, name)
        elif isinstance(obj, dict) and name in obj:
            val = obj[name]
        else:
            try:
                val = obj[name]
            except Exception:
                val = default
        if hasattr(val, "item"):
            try:
                return val.item()
            except Exception:
                return val
        return val

    def _int_field(self, obj, name: str, default: int = 0) -> int:
        """读取整数字段，0 值安全（不会被 ``or`` 误吞）。"""
        val = self._get_field(obj, name)
        return int(val) if val is not None else default

    @contextmanager
    def _measure(self, name: str):
        """记录单次调用耗时/错误的上下文管理器。"""
        start = time.time()
        error = False
        try:
            yield
        except Exception:
            error = True
            raise
        finally:
            self.metrics.record(name, (time.time() - start) * 1000, error=error)

    def metrics_snapshot(self) -> dict:
        return self.metrics.snapshot()

    @staticmethod
    def measured(name: Optional[str] = None):
        """
        装饰器：记录方法耗时与错误次数，不入侵业务代码。
        """

        def decorator(fn):
            metric_name = name or fn.__name__

            @wraps(fn)
            def wrapper(self, *args, **kwargs):
                with self._measure(metric_name):
                    return fn(self, *args, **kwargs)

            return wrapper

        return decorator
