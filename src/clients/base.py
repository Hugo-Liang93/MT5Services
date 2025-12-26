"""
MT5 客户端基类：统一初始化/登录、时区转换、字段提取等通用逻辑。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from typing import Optional
from zoneinfo import ZoneInfo

from src.config import MT5Settings, load_mt5_settings

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None


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


class MT5BaseClient:
    def __init__(self, settings: Optional[MT5Settings] = None):
        self.settings = settings or load_mt5_settings()
        self.tz = ZoneInfo(self.settings.timezone)
        self._connected = False
        self.metrics = MetricsRecorder()

    def connect(self):
        if mt5 is None:
            raise MT5BaseError("MetaTrader5 package is not installed")
        if self._connected:
            return
        if not mt5.initialize(path=self.settings.mt5_path or None):
            raise MT5BaseError(f"Failed to initialize MT5: {mt5.last_error()}")
        if self.settings.mt5_login:
            authorized = mt5.login(
                login=self.settings.mt5_login,
                password=self.settings.mt5_password or "",
                server=self.settings.mt5_server or "",
            )
            if not authorized:
                raise MT5BaseError(f"Failed to login to MT5: {mt5.last_error()}")
        self._connected = True

    def shutdown(self):
        if self._connected and mt5:
            mt5.shutdown()
        self._connected = False

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
