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
from src.ops.mt5_session_gate import ensure_mt5_session_gate_or_raise

logger = logging.getLogger("mt5services.supervisor")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run MT5Services supervisor for a topology group"
    )
    group_selector = parser.add_mutually_exclusive_group(required=True)
    group_selector.add_argument("--group", help="topology group name, e.g. live")
    group_selector.add_argument(
        "--environment", help="运行环境，当前等价于 topology group（live/demo）"
    )
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


def _classify_exit_code(exit_code: int | None) -> str:
    """将进程退出码分类为重启策略类型。

    Returns:
        "transient" — 临时故障（网络/MT5 断连），适合快速重试
        "fatal"     — 致命错误（段错误/OOM），适合等待 gate 检查
        "normal"    — 正常退出，不应重启
        "unknown"   — 无法分类
    """
    if exit_code is None:
        return "unknown"
    if exit_code == 0:
        return "normal"
    # Windows: 负退出码 = 未处理异常信号（如 -1073741819 = ACCESS_VIOLATION）
    # Linux: 128+N = 被信号 N 终止（137=SIGKILL, 139=SIGSEGV, 134=SIGABRT）
    if exit_code < 0 or exit_code >= 128:
        return "fatal"
    # 1~127: 普通错误退出（初始化失败、运行时异常等）
    return "transient"


@dataclass
class ManagedProcess:
    instance_name: str
    process: subprocess.Popen[str]
    restart_count: int = 0
    last_start_monotonic: float = field(default_factory=time.monotonic)


class Supervisor:
    # 启动时 gate 失败的 worker 会被放入 _pending_workers，_monitor_loop 周期性重试。
    # 间隔选 30s：平衡"用户启动 terminal 后多快能自动补齐"与"频繁 gate 探测开销"。
    _PENDING_WORKER_RETRY_INTERVAL_SECONDS = 30.0

    def __init__(self, group: TopologyGroup, *, ready_timeout_seconds: float) -> None:
        self._group = group
        self._ready_timeout_seconds = ready_timeout_seconds
        self._managed: dict[str, ManagedProcess] = {}
        self._stopping = False
        # 启动期 gate 失败但未彻底放弃的 workers —— monitor_loop 会周期性重试它们，
        # 让用户启动 MT5 terminal 后不需要重启整个 supervisor 即可自动补齐。
        self._pending_workers: set[str] = set()
        self._last_pending_retry_at: float = 0.0

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
        # Main gate 必须通过：main 是数据源/信号源，没有 main 整个链路无法工作。
        ensure_mt5_session_gate_or_raise(instance_name=self._group.main)
        logger.info("MT5 session gate passed for main instance=%s", self._group.main)
        self._managed[self._group.main] = self._spawn(self._group.main)
        _wait_for_ready(self._group.main, self._ready_timeout_seconds)

        # Worker gate 失败仅 warn，不阻断 main 启动。
        # 工作线: workers 是消费 execution_intents 的执行器，缺失只意味着部分账户暂时无法下单，
        # 不影响 main 的指标/信号链路；worker 本身的 instance entrypoint 会再做一次 gate 检查并 fail-fast，
        # 等 user 把对应 MT5 terminal 启动起来后通过 supervisor 重启即可补齐。
        for worker in self._group.workers:
            try:
                ensure_mt5_session_gate_or_raise(instance_name=worker)
            except RuntimeError as exc:
                logger.warning(
                    "Worker %s skipped at startup: %s. "
                    "Main remains operational; supervisor will retry every %.0fs "
                    "once the worker terminal becomes reachable.",
                    worker,
                    exc,
                    self._PENDING_WORKER_RETRY_INTERVAL_SECONDS,
                )
                self._pending_workers.add(worker)
                continue
            self._managed[worker] = self._spawn(worker)

    def _monitor_loop(self) -> None:
        while not self._stopping:
            for instance_name, managed in list(self._managed.items()):
                exit_code = managed.process.poll()
                if exit_code is None:
                    continue
                if self._stopping:
                    return
                exit_class = _classify_exit_code(exit_code)
                if exit_class == "normal":
                    logger.info(
                        "Instance exited normally: instance=%s code=0",
                        instance_name,
                    )
                    continue
                log_fn = logger.error if exit_class == "fatal" else logger.warning
                log_fn(
                    "Instance exited unexpectedly: instance=%s code=%s "
                    "class=%s restart_count=%s",
                    instance_name,
                    exit_code,
                    exit_class,
                    managed.restart_count,
                )
                # fatal 错误（段错误/OOM）使用更长的冷却期
                if exit_class == "fatal":
                    delay_seconds = min(
                        60.0,
                        max(5.0, float(2 ** min(managed.restart_count + 1, 5))),
                    )
                else:
                    delay_seconds = min(
                        30.0,
                        max(1.0, float(2 ** min(managed.restart_count, 4))),
                    )
                time.sleep(delay_seconds)
                # 重启前先做 gate 检查；非 main 实例 gate 失败时仅记 warning 等下次重试。
                try:
                    ensure_mt5_session_gate_or_raise(instance_name=instance_name)
                except RuntimeError as exc:
                    if instance_name == self._group.main:
                        # main 不可恢复 → 终止整组
                        logger.error(
                            "Main instance gate failed during restart: %s", exc
                        )
                        raise
                    logger.warning(
                        "Worker %s gate failed during restart, will retry next loop: %s",
                        instance_name,
                        exc,
                    )
                    # Worker gate 失败：累加 restart_count 让下次重试用更大的指数回退
                    # 间隔（与正常 worker 退出后的重试延迟逻辑一致，复用 line 158 的
                    # 2^min(count, 4) 公式）。保留旧 process 引用，避免下次循环误入
                    # "新进程"路径。
                    self._managed[instance_name] = ManagedProcess(
                        instance_name=instance_name,
                        process=managed.process,
                        restart_count=managed.restart_count + 1,
                    )
                    continue
                restarted = self._spawn(
                    instance_name, restart_count=managed.restart_count + 1
                )
                self._managed[instance_name] = restarted
                if instance_name == self._group.main:
                    _wait_for_ready(instance_name, self._ready_timeout_seconds)
            # 周期性重试启动期被跳过的 workers。用 monotonic 节流避免每秒打 gate。
            self._retry_pending_workers()
            time.sleep(1.0)

    def _retry_pending_workers(self) -> None:
        """周期性重试启动期 gate 失败的 workers。

        一旦 gate 通过就 spawn 进程并从 _pending_workers 移除。
        间隔由 _PENDING_WORKER_RETRY_INTERVAL_SECONDS 控制，避免频繁探测开销。
        """
        if not self._pending_workers:
            return
        now = time.monotonic()
        if (
            now - self._last_pending_retry_at
            < self._PENDING_WORKER_RETRY_INTERVAL_SECONDS
        ):
            return
        self._last_pending_retry_at = now
        for worker in list(self._pending_workers):
            if self._stopping:
                return
            try:
                ensure_mt5_session_gate_or_raise(instance_name=worker)
            except RuntimeError as exc:
                logger.debug("Pending worker %s still unreachable: %s", worker, exc)
                continue
            logger.info("Pending worker %s gate now passes, spawning", worker)
            try:
                self._managed[worker] = self._spawn(worker)
                self._pending_workers.discard(worker)
            except Exception:
                logger.warning(
                    "Pending worker %s spawn failed, will retry later",
                    worker,
                    exc_info=True,
                )

    def _spawn(self, instance_name: str, *, restart_count: int = 0) -> ManagedProcess:
        # gate 检查由调用方负责（_start_initial / _monitor_loop），
        # 这里只负责进程创建，避免重复检查 + 提高弹性（worker 缺失 terminal 不阻断 main）。
        command = [
            sys.executable,
            "-m",
            "src.entrypoint.instance",
            "--instance",
            instance_name,
        ]
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
    # UTF-8 stdio 必须在配置 logging 前调用，保证 supervisor 自己的 stdout 也是 UTF-8
    from src.entrypoint.web import _force_utf8_stdio

    _force_utf8_stdio()
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
