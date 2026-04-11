"""交易模块共享线程生命周期工具。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class OwnedThreadLifecycle:
    """管理单个后台线程的启动/停止生命周期。"""

    def __init__(self, owner: Any, thread_attr: str, *, label: str) -> None:
        self._owner = owner
        self._thread_attr = thread_attr
        self._label = label

    @property
    def _thread(self) -> threading.Thread | None:
        return getattr(self._owner, self._thread_attr, None)

    @_thread.setter
    def _thread(self, value: threading.Thread | None) -> None:
        setattr(self._owner, self._thread_attr, value)

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def ensure_running(self, thread_factory: Callable[[], threading.Thread]) -> bool:
        """如果当前线程不存在或已退出，则按工厂函数创建并启动。"""
        if self.is_running():
            return False
        thread = thread_factory()
        thread.start()
        self._thread = thread
        return True

    def wait_previous(self, timeout: float = 5.0) -> bool:
        """等待旧线程退出并清理引用。"""
        thread = self._thread
        if thread is None:
            return True
        if not thread.is_alive():
            self._thread = None
            return True

        logger.warning("%s: waiting for previous thread to finish", self._label)
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.error(
                "%s: previous thread still alive after re-join",
                self._label,
            )
            return False

        self._thread = None
        return True

    def stop(self, stop_event: threading.Event, timeout: float) -> bool:
        """置位停止事件并等待线程退出，不成功则保留线程引用。"""
        stop_event.set()
        stopped = self.wait_previous(timeout=timeout)
        if not stopped:
            logger.warning(
                "%s: worker did not stop within %.1fs, will be cleaned up on next start()",
                self._label,
                timeout,
            )
        return stopped

