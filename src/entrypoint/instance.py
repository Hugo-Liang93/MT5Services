from __future__ import annotations

import argparse
import logging
import os
import threading
from pathlib import Path

from src.config import reload_configs
from src.config.instance_context import (
    get_current_instance_name,
    resolve_instance_config_dir,
    set_current_environment,
    set_current_instance_name,
)
from src.config.topology import resolve_topology_assignment
from src.entrypoint.web import launch

_logger = logging.getLogger(__name__)


def _start_supervisor_watchdog(supervisor_pid: int, poll_interval: float = 5.0) -> None:
    """P9 bug #4: supervisor 被外部 /F 强杀时不会触发子进程 cleanup，
    子进程会成孤儿并继续持有 SQLite 锁 + 端口。本 watchdog 在子进程内
    周期检查 supervisor 是否仍存活；supervisor 死时子进程主动退出释放资源。

    跨平台实现：用 os.kill(pid, 0) 探活（Linux/Windows 都支持，0 信号不发实际信号
    只检查权限/存活）。检查失败抛 OSError → supervisor 已死 → 自杀。
    """
    if supervisor_pid <= 0:
        return

    def _watch() -> None:
        import time

        while True:
            try:
                os.kill(supervisor_pid, 0)
            except (ProcessLookupError, OSError):
                _logger.warning(
                    "Supervisor pid=%d gone; instance exiting to avoid orphaned state",
                    supervisor_pid,
                )
                # os._exit 强退让 OS 释放所有 fd / 端口（避免 atexit 卡住或 Python
                # 同步 cleanup 阻塞 SQLite WAL checkpoint）
                os._exit(0)
            time.sleep(poll_interval)

    thread = threading.Thread(
        target=_watch,
        name="supervisor-watchdog",
        daemon=True,
    )
    thread.start()
    _logger.info(
        "Supervisor watchdog started: parent_pid=%d poll=%.1fs",
        supervisor_pid,
        poll_interval,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch a named MT5Services instance")
    parser.add_argument(
        "--instance", required=False, help="实例名，对应 config/instances/<instance>"
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    instance_name = set_current_instance_name(
        args.instance or get_current_instance_name()
    )
    if not instance_name:
        raise SystemExit(
            "missing instance name; use --instance <name> or set MT5_INSTANCE"
        )
    instance_dir = resolve_instance_config_dir(instance_name=instance_name)
    if instance_dir is None:
        raise SystemExit(
            f"instance config directory not found: {Path('config') / 'instances' / instance_name}"
        )
    assignment = resolve_topology_assignment(instance_name)
    if assignment is None:
        raise SystemExit(f"instance {instance_name} is not configured in topology.ini")
    set_current_environment(assignment.environment)
    reload_configs()
    # P9 bug #4: 由 supervisor 启动时通过 env 传 pid，独立启动时无该 env → 不启 watchdog
    supervisor_pid_env = os.environ.get("MT5_SUPERVISOR_PID", "").strip()
    if supervisor_pid_env:
        try:
            _start_supervisor_watchdog(int(supervisor_pid_env))
        except ValueError:
            _logger.warning(
                "Ignoring invalid MT5_SUPERVISOR_PID=%r", supervisor_pid_env
            )
    launch()


if __name__ == "__main__":
    main()
