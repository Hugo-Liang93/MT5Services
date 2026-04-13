"""
MT5 客户端基类：统一初始化/登录、时区转换、字段提取等通用逻辑。
"""

from __future__ import annotations

import logging
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.config import MT5Settings, load_mt5_settings

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


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


@dataclass(frozen=True)
class MT5SessionState:
    terminal_reachable: bool
    terminal_process_ready: bool
    ipc_ready: bool
    authorized: bool
    account_match: bool
    session_ready: bool
    interactive_login_required: bool
    error_code: str | None
    error_message: str | None
    terminal_name: str | None = None
    terminal_path: str | None = None
    login: int | None = None
    server: str | None = None
    requested_login: int | None = None
    requested_server: str | None = None
    last_error_code: int | None = None
    last_error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "terminal_reachable": self.terminal_reachable,
            "terminal_process_ready": self.terminal_process_ready,
            "ipc_ready": self.ipc_ready,
            "authorized": self.authorized,
            "account_match": self.account_match,
            "session_ready": self.session_ready,
            "interactive_login_required": self.interactive_login_required,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "terminal_name": self.terminal_name,
            "terminal_path": self.terminal_path,
            "login": self.login,
            "server": self.server,
            "requested_login": self.requested_login,
            "requested_server": self.requested_server,
            "last_error": {
                "code": self.last_error_code,
                "message": self.last_error_message,
            },
        }


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
        self._last_session_state: MT5SessionState | None = None
        self.metrics = MetricsRecorder()
        configured_offset_hours = getattr(self.settings, "server_time_offset_hours", None)
        self._configured_market_time_offset_seconds: Optional[int] = (
            int(configured_offset_hours) * 3600 if configured_offset_hours is not None else None
        )
        self._market_time_offset_seconds: Optional[int] = self._configured_market_time_offset_seconds

    @staticmethod
    def _normalize_fs_path(path: str | None) -> str:
        raw = str(path or "").strip()
        if not raw:
            return ""
        try:
            return str(Path(raw).expanduser().resolve()).lower()
        except OSError:
            return raw.lower()

    @classmethod
    def _terminal_process_running(cls, terminal_path: str | None) -> bool:
        normalized_path = cls._normalize_fs_path(terminal_path)
        if not normalized_path or psutil is None:
            return False
        basename = Path(normalized_path).name.lower()
        try:
            for proc in psutil.process_iter(["name", "exe"]):
                try:
                    exe = proc.info.get("exe")
                    if exe and cls._normalize_fs_path(exe) == normalized_path:
                        return True
                    name = str(proc.info.get("name") or "").strip().lower()
                    if name and name == basename and not exe:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            logger.debug("Failed to inspect MT5 terminal process state", exc_info=True)
            return False
        return False

    def _login_required(self) -> bool:
        return self.settings.mt5_login is not None

    def _terminal_path_exists(self) -> bool:
        configured_path = str(self.settings.mt5_path or "").strip()
        if not configured_path:
            return False
        try:
            return Path(configured_path).expanduser().exists()
        except OSError:
            return False

    @staticmethod
    def _safe_last_error() -> tuple[int | None, str | None]:
        if mt5 is None:
            return None, None
        try:
            raw = mt5.last_error()
        except Exception:
            return None, None
        if isinstance(raw, tuple):
            code = raw[0] if len(raw) > 0 else None
            message = raw[1] if len(raw) > 1 else None
        else:
            code = None
            message = raw
        try:
            normalized_code = int(code) if code is not None else None
        except (TypeError, ValueError):
            normalized_code = None
        normalized_message = (
            str(message).strip() if message not in (None, "") else None
        )
        return normalized_code, normalized_message

    @staticmethod
    def _safe_terminal_info():
        if mt5 is None:
            return None
        try:
            return mt5.terminal_info()
        except Exception:
            return None

    @staticmethod
    def _safe_account_info():
        if mt5 is None:
            return None
        try:
            return mt5.account_info()
        except Exception:
            return None

    def _matches_requested_account(self, account_info: Any = None) -> bool:
        account_info = account_info if account_info is not None else self._safe_account_info()
        if account_info is None:
            return not self._login_required()

        current_login = getattr(account_info, "login", None)
        current_server = getattr(account_info, "server", None)
        if self._login_required() and current_login != self.settings.mt5_login:
            return False
        if self.settings.mt5_server and current_server != self.settings.mt5_server:
            return False
        return True

    def _session_ready(self) -> bool:
        state = self.inspect_session_state(
            require_terminal_process=False,
            attempt_initialize=False,
            attempt_login=False,
        )
        return state.session_ready

    def _initialize_kwargs(self) -> dict[str, Any]:
        initialize_kwargs: dict[str, Any] = {}
        if self.settings.mt5_path:
            initialize_kwargs["path"] = self.settings.mt5_path
        return initialize_kwargs

    def _login_kwargs(self) -> dict[str, Any]:
        login_kwargs: dict[str, Any] = {}
        if self.settings.mt5_login is not None:
            login_kwargs["login"] = self.settings.mt5_login
        if self.settings.mt5_password:
            login_kwargs["password"] = self.settings.mt5_password
        if self.settings.mt5_server:
            login_kwargs["server"] = self.settings.mt5_server
        return login_kwargs

    @staticmethod
    def _is_ipc_timeout(last_error_code: int | None, last_error_message: str | None) -> bool:
        message = str(last_error_message or "").strip().lower()
        return bool(
            last_error_code == -10005
            or "ipc timeout" in message
            or "ipc send failed" in message
        )

    def _is_interactive_login_required(
        self,
        *,
        last_error_code: int | None,
        last_error_message: str | None,
        terminal_process_ready: bool,
    ) -> bool:
        return bool(
            terminal_process_ready
            and self._login_required()
            and self._is_ipc_timeout(last_error_code, last_error_message)
        )

    @staticmethod
    def _account_snapshot(account_info: Any) -> tuple[int | None, str | None]:
        if account_info is None:
            return None, None
        login = getattr(account_info, "login", None)
        server = getattr(account_info, "server", None)
        try:
            login = int(login) if login is not None else None
        except (TypeError, ValueError):
            login = None
        return login, str(server).strip() if server not in (None, "") else None

    def _session_error_message(
        self,
        *,
        error_code: str,
        actual_login: int | None,
        actual_server: str | None,
        last_error_message: str | None,
    ) -> str:
        if error_code == "package_missing":
            return "MetaTrader5 package is not installed"
        if error_code == "terminal_path_missing":
            return "MT5 terminal path is not configured"
        if error_code == "terminal_not_found":
            return f"MT5 terminal path is not reachable: {self.settings.mt5_path}"
        if error_code == "terminal_not_running":
            return f"MT5 terminal process is not running: {self.settings.mt5_path}"
        if error_code == "interactive_login_required":
            return (
                "MT5 terminal requires interactive login/unlock before startup"
            )
        if error_code == "ipc_timeout":
            return f"MT5 IPC is not ready: {last_error_message or 'timeout'}"
        if error_code == "initialize_failed":
            return f"MT5 initialize() failed: {last_error_message or 'unknown error'}"
        if error_code == "login_failed":
            requested_login = self.settings.mt5_login
            requested_server = self.settings.mt5_server
            return (
                "MT5 login failed for "
                f"{requested_login}@{requested_server or 'unknown-server'}: "
                f"{last_error_message or 'unknown error'}"
            )
        if error_code == "account_mismatch":
            return (
                "MT5 account mismatch: expected "
                f"{self.settings.mt5_login}@{self.settings.mt5_server or 'unknown-server'}, "
                f"got {actual_login}@{actual_server or 'unknown-server'}"
            )
        return last_error_message or "MT5 session is not ready"

    def _build_session_state(
        self,
        *,
        terminal_reachable: bool,
        terminal_process_ready: bool,
        ipc_ready: bool,
        account_info: Any,
        terminal_info: Any,
        interactive_login_required: bool,
        error_code: str | None,
        error_message: str | None,
        last_error_code: int | None,
        last_error_message: str | None,
    ) -> MT5SessionState:
        actual_login, actual_server = self._account_snapshot(account_info)
        authorized = bool(account_info is not None or not self._login_required())
        account_match = self._matches_requested_account(account_info)
        session_ready = bool(
            terminal_reachable
            and ipc_ready
            and authorized
            and account_match
            and not interactive_login_required
            and error_code is None
        )
        state = MT5SessionState(
            terminal_reachable=terminal_reachable,
            terminal_process_ready=terminal_process_ready,
            ipc_ready=ipc_ready,
            authorized=authorized,
            account_match=account_match,
            session_ready=session_ready,
            interactive_login_required=interactive_login_required,
            error_code=error_code,
            error_message=error_message,
            terminal_name=getattr(terminal_info, "name", None) if terminal_info is not None else None,
            terminal_path=self.settings.mt5_path,
            login=actual_login,
            server=actual_server,
            requested_login=self.settings.mt5_login,
            requested_server=self.settings.mt5_server,
            last_error_code=last_error_code,
            last_error_message=last_error_message,
        )
        self._last_session_state = state
        return state

    def inspect_session_state(
        self,
        *,
        require_terminal_process: bool = True,
        attempt_initialize: bool = True,
        attempt_login: bool = True,
        shutdown_after_probe: bool = False,
    ) -> MT5SessionState:
        if mt5 is None:
            return self._build_session_state(
                terminal_reachable=False,
                terminal_process_ready=False,
                ipc_ready=False,
                account_info=None,
                terminal_info=None,
                interactive_login_required=False,
                error_code="package_missing",
                error_message=self._session_error_message(
                    error_code="package_missing",
                    actual_login=None,
                    actual_server=None,
                    last_error_message=None,
                ),
                last_error_code=None,
                last_error_message=None,
            )

        terminal_info = self._safe_terminal_info()
        account_info = self._safe_account_info()
        terminal_reachable = bool(terminal_info is not None or self._terminal_path_exists())
        terminal_process_ready = bool(
            terminal_info is not None or self._terminal_process_running(self.settings.mt5_path)
        )
        ipc_ready = bool(terminal_info is not None)
        initialized_here = False

        try:
            if not terminal_reachable and terminal_info is None:
                return self._build_session_state(
                    terminal_reachable=False,
                    terminal_process_ready=terminal_process_ready,
                    ipc_ready=False,
                    account_info=account_info,
                    terminal_info=terminal_info,
                    interactive_login_required=False,
                    error_code="terminal_not_found"
                    if self.settings.mt5_path
                    else "terminal_path_missing",
                    error_message=self._session_error_message(
                        error_code="terminal_not_found"
                        if self.settings.mt5_path
                        else "terminal_path_missing",
                        actual_login=None,
                        actual_server=None,
                        last_error_message=None,
                    ),
                    last_error_code=None,
                    last_error_message=None,
                )

            if require_terminal_process and not terminal_process_ready and terminal_info is None:
                return self._build_session_state(
                    terminal_reachable=terminal_reachable,
                    terminal_process_ready=False,
                    ipc_ready=False,
                    account_info=account_info,
                    terminal_info=terminal_info,
                    interactive_login_required=False,
                    error_code="terminal_not_running",
                    error_message=self._session_error_message(
                        error_code="terminal_not_running",
                        actual_login=None,
                        actual_server=None,
                        last_error_message=None,
                    ),
                    last_error_code=None,
                    last_error_message=None,
                )

            if attempt_initialize and not ipc_ready:
                if not mt5.initialize(**self._initialize_kwargs()):
                    last_error_code, last_error_message = self._safe_last_error()
                    interactive_login_required = self._is_interactive_login_required(
                        last_error_code=last_error_code,
                        last_error_message=last_error_message,
                        terminal_process_ready=terminal_process_ready,
                    )
                    error_code = (
                        "interactive_login_required"
                        if interactive_login_required
                        else "ipc_timeout"
                        if self._is_ipc_timeout(last_error_code, last_error_message)
                        else "initialize_failed"
                    )
                    return self._build_session_state(
                        terminal_reachable=terminal_reachable,
                        terminal_process_ready=terminal_process_ready,
                        ipc_ready=False,
                        account_info=account_info,
                        terminal_info=terminal_info,
                        interactive_login_required=interactive_login_required,
                        error_code=error_code,
                        error_message=self._session_error_message(
                            error_code=error_code,
                            actual_login=None,
                            actual_server=None,
                            last_error_message=last_error_message,
                        ),
                        last_error_code=last_error_code,
                        last_error_message=last_error_message,
                    )
                initialized_here = True
                terminal_info = self._safe_terminal_info()
                account_info = self._safe_account_info()
                terminal_reachable = True
                terminal_process_ready = True
                ipc_ready = bool(terminal_info is not None)

            if not ipc_ready:
                last_error_code, last_error_message = self._safe_last_error()
                error_code = (
                    "ipc_timeout"
                    if self._is_ipc_timeout(last_error_code, last_error_message)
                    else "initialize_failed"
                )
                return self._build_session_state(
                    terminal_reachable=terminal_reachable,
                    terminal_process_ready=terminal_process_ready,
                    ipc_ready=False,
                    account_info=account_info,
                    terminal_info=terminal_info,
                    interactive_login_required=False,
                    error_code=error_code,
                    error_message=self._session_error_message(
                        error_code=error_code,
                        actual_login=None,
                        actual_server=None,
                        last_error_message=last_error_message,
                    ),
                    last_error_code=last_error_code,
                    last_error_message=last_error_message,
                )

            if attempt_login and self._login_required() and not self._matches_requested_account(account_info):
                if not mt5.login(**self._login_kwargs()):
                    last_error_code, last_error_message = self._safe_last_error()
                    interactive_login_required = self._is_interactive_login_required(
                        last_error_code=last_error_code,
                        last_error_message=last_error_message,
                        terminal_process_ready=terminal_process_ready,
                    )
                    error_code = (
                        "interactive_login_required"
                        if interactive_login_required
                        else "login_failed"
                    )
                    actual_login, actual_server = self._account_snapshot(account_info)
                    return self._build_session_state(
                        terminal_reachable=terminal_reachable,
                        terminal_process_ready=terminal_process_ready,
                        ipc_ready=True,
                        account_info=account_info,
                        terminal_info=terminal_info,
                        interactive_login_required=interactive_login_required,
                        error_code=error_code,
                        error_message=self._session_error_message(
                            error_code=error_code,
                            actual_login=actual_login,
                            actual_server=actual_server,
                            last_error_message=last_error_message,
                        ),
                        last_error_code=last_error_code,
                        last_error_message=last_error_message,
                    )
                account_info = self._safe_account_info()

            actual_login, actual_server = self._account_snapshot(account_info)
            if self._login_required() and not self._matches_requested_account(account_info):
                return self._build_session_state(
                    terminal_reachable=terminal_reachable,
                    terminal_process_ready=terminal_process_ready,
                    ipc_ready=True,
                    account_info=account_info,
                    terminal_info=terminal_info,
                    interactive_login_required=False,
                    error_code="account_mismatch",
                    error_message=self._session_error_message(
                        error_code="account_mismatch",
                        actual_login=actual_login,
                        actual_server=actual_server,
                        last_error_message=None,
                    ),
                    last_error_code=None,
                    last_error_message=None,
                )

            return self._build_session_state(
                terminal_reachable=terminal_reachable,
                terminal_process_ready=terminal_process_ready,
                ipc_ready=True,
                account_info=account_info,
                terminal_info=terminal_info,
                interactive_login_required=False,
                error_code=None,
                error_message=None,
                last_error_code=None,
                last_error_message=None,
            )
        finally:
            if shutdown_after_probe and initialized_here:
                try:
                    mt5.shutdown()
                except Exception:
                    logger.debug("Failed to shutdown MT5 after probe", exc_info=True)

    def session_state(self) -> MT5SessionState | None:
        return self._last_session_state

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

            state = self.inspect_session_state(
                require_terminal_process=True,
                attempt_initialize=True,
                attempt_login=True,
            )
            if not state.session_ready:
                self._connected = False
                raise MT5BaseError(
                    f"MT5 session not ready [{state.error_code or 'unknown'}]: "
                    f"{state.error_message or 'unknown error'}"
                )
            self._connected = True
            MT5BaseClient._last_session_check = time.monotonic()

    def shutdown(self):
        if self._connected and mt5:
            with self._session_lock:
                mt5.shutdown()
        self._connected = False
        self._last_session_state = None
        self._market_time_offset_seconds = self._configured_market_time_offset_seconds

    def health(self) -> dict:
        """基础健康检查：连接状态、账户/终端信息。"""
        state = self.inspect_session_state(
            require_terminal_process=True,
            attempt_initialize=True,
            attempt_login=True,
        )
        payload = {
            "connected": state.session_ready,
            "login": state.login,
            "server": state.server,
            "terminal": state.terminal_name,
            "mt5_session": state.to_dict(),
        }
        if not state.session_ready:
            payload["error"] = state.error_message
        return payload

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
