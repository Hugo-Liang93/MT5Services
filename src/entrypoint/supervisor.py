from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.request import urlopen

from src.config.topology import TopologyGroup, load_topology_group
from src.config.utils import load_config_with_base
from src.entrypoint.logging_context import (
    inject_logging_context,
    install_log_record_context,
)

logger = logging.getLogger("mt5services.supervisor")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run MT5Services supervisor for a topology group")
    group_selector = parser.add_mutually_exclusive_group(required=True)
    group_selector.add_argument("--group", help="topology group name, e.g. live")
    group_selector.add_argument("--environment", help="运行环境，当前等价于 topology group（live/demo）")
    parser.add_argument(
        "--ready-timeout-seconds",
        type=float,
        default=60.0,
        help="等待 main ready 的超时时间",
    )
    return parser.parse_args()


def _api_target(instance_name: str) -> tuple[str, int]:
    _, parser = load_config_with_base("market.ini", instance_name=instance_name)
    if parser is None:
        raise RuntimeError(f"missing market config for instance {instance_name}")
    host = "127.0.0.1"
    port = 8808
    if parser.has_section("api"):
        host = str(parser["api"].get("host", host)).strip() or host
        port = int(parser["api"].get("port", port))
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return host, port


def _wait_for_ready(instance_name: str, timeout_seconds: float) -> None:
    host, port = _api_target(instance_name)
    deadline = time.time() + max(5.0, timeout_seconds)
    url = f"http://{host}:{port}/v1/monitoring/health/ready"
    last_error: str | None = None
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2.0) as response:
                if 200 <= int(response.status) < 300:
                    return
        except (OSError, URLError) as exc:
            last_error = str(exc)
        time.sleep(1.0)
    raise RuntimeError(
        f"instance {instance_name} did not become ready within {timeout_seconds:.0f}s"
        + (f": {last_error}" if last_error else "")
    )


@dataclass
class ManagedProcess:
    instance_name: str
    process: subprocess.Popen[str]
    restart_count: int = 0
    last_start_monotonic: float = field(default_factory=time.monotonic)


class Supervisor:
    def __init__(self, group: TopologyGroup, *, ready_timeout_seconds: float) -> None:
        self._group = group
        self._ready_timeout_seconds = ready_timeout_seconds
        self._managed: dict[str, ManagedProcess] = {}
        self._stopping = False

    def run(self) -> None:
        try:
            self._start_initial()
            self._monitor_loop()
        except KeyboardInterrupt:
            logger.info("Supervisor interrupted, stopping children")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stopping = True
        for managed in list(self._managed.values()):
            self._terminate(managed.process)
        for managed in list(self._managed.values()):
            try:
                managed.process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                managed.process.kill()
        self._managed.clear()

    def _start_initial(self) -> None:
        logger.info("Starting topology group=%s", self._group.name)
        self._managed[self._group.main] = self._spawn(self._group.main)
        _wait_for_ready(self._group.main, self._ready_timeout_seconds)
        for worker in self._group.workers:
            self._managed[worker] = self._spawn(worker)

    def _monitor_loop(self) -> None:
        while not self._stopping:
            for instance_name, managed in list(self._managed.items()):
                exit_code = managed.process.poll()
                if exit_code is None:
                    continue
                if self._stopping:
                    return
                logger.warning(
                    "Instance exited unexpectedly: instance=%s code=%s restart_count=%s",
                    instance_name,
                    exit_code,
                    managed.restart_count,
                )
                delay_seconds = min(30.0, max(1.0, float(2 ** min(managed.restart_count, 4))))
                time.sleep(delay_seconds)
                restarted = self._spawn(instance_name, restart_count=managed.restart_count + 1)
                self._managed[instance_name] = restarted
                if instance_name == self._group.main:
                    _wait_for_ready(instance_name, self._ready_timeout_seconds)
            time.sleep(1.0)

    def _spawn(self, instance_name: str, *, restart_count: int = 0) -> ManagedProcess:
        command = [sys.executable, "-m", "src.entrypoint.instance", "--instance", instance_name]
        env = dict(os.environ)
        env["MT5_INSTANCE"] = instance_name
        env["MT5_ENVIRONMENT"] = self._group.environment
        process = subprocess.Popen(
            command,
            cwd=str(_project_root()),
            env=env,
            creationflags=self._creationflags(),
        )
        logger.info("Started instance=%s pid=%s", instance_name, process.pid)
        return ManagedProcess(
            instance_name=instance_name,
            process=process,
            restart_count=restart_count,
        )

    def _creationflags(self) -> int:
        if os.name != "nt":
            return 0
        return subprocess.CREATE_NEW_PROCESS_GROUP

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
        except Exception:
            process.terminate()


def main() -> None:
    args = _parse_args()
    group_name = str(args.group or args.environment or "").strip()
    group = load_topology_group(group_name)
    install_log_record_context(
        instance_name="supervisor",
        environment=group.environment,
        role="supervisor",
    )
    logging.basicConfig(
        level=logging.INFO,
        format=inject_logging_context("%(asctime)s %(levelname)s %(name)s %(message)s"),
    )
    supervisor = Supervisor(group, ready_timeout_seconds=args.ready_timeout_seconds)
    supervisor.run()


if __name__ == "__main__":
    main()
