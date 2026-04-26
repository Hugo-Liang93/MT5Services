"""启动后健康巡检 CLI — 验证运行中的实例各项指标正常。

用法：
    python -m src.ops.cli.health_check
    python -m src.ops.cli.health_check --instance live-main
    python -m src.ops.cli.health_check --host 127.0.0.1 --port 8808
    python -m src.ops.cli.health_check --wait 30    # 等待最多 30s 直到 ready

设计原则：纯 HTTP 请求，不启动服务、不导入业务代码。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def _api_target(args: argparse.Namespace) -> tuple[str, int]:
    """解析 API 地址。优先用 --host/--port，否则从实例配置读取。"""
    if args.host and args.port:
        return args.host, args.port
    if args.instance:
        try:
            from src.config.utils import load_config_with_base

            _, parser = load_config_with_base("market.ini", instance_name=args.instance)
            if parser and parser.has_section("api"):
                host = str(parser["api"].get("host", "127.0.0.1")).strip()
                port = int(parser["api"].get("port", 8808))
                if host == "0.0.0.0":
                    host = "127.0.0.1"
                return host, port
        except Exception:
            pass
    return "127.0.0.1", 8808


def _fetch_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _wait_for_ready(host: str, port: int, timeout: float) -> bool:
    """等待实例就绪，返回是否成功。"""
    url = f"http://{host}:{port}/v1/monitoring/health/ready"
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            _fetch_json(url, timeout=2.0)
            return True
        except Exception as exc:
            last_error = str(exc)
            time.sleep(1.0)
    sys.stderr.write(f"  Timeout waiting for ready: {last_error}\n")
    return False


def _run_checks(host: str, port: int) -> list[tuple[str, str, str]]:
    """执行所有健康检查，返回 [(名称, 状态, 详情)]。"""
    results: list[tuple[str, str, str]] = []
    base = f"http://{host}:{port}"

    # 1. Ready 探针
    try:
        ready = _fetch_json(f"{base}/v1/monitoring/health/ready")
        status = ready.get("status", "unknown")
        checks = ready.get("checks", {})
        if status == "ready":
            results.append(("Ready probe", "OK", f"checks={checks}"))
        else:
            results.append(("Ready probe", "FAIL", f"status={status}"))
    except Exception as exc:
        results.append(("Ready probe", "FAIL", str(exc)))
        return results  # 如果 ready 失败，后续检查无意义

    # 2. 健康报告（含 active_alerts）
    try:
        health_resp = _fetch_json(f"{base}/v1/monitoring/health")
        data = health_resp.get("data", {})
        overall = data.get("overall_status", "unknown")
        active_alerts = data.get("active_alerts", {})
        if overall == "healthy":
            results.append(("Overall health", "OK", "healthy"))
        elif overall == "warning":
            results.append(
                (
                    "Overall health",
                    "WARN",
                    f"{len(active_alerts)} active alerts",
                )
            )
        else:
            results.append(
                (
                    "Overall health",
                    "FAIL",
                    f"status={overall}, alerts={list(active_alerts.keys())}",
                )
            )
        # 逐项检查告警
        for alert_key, alert_info in active_alerts.items():
            level = alert_info.get("alert_level", "?")
            msg = alert_info.get("message", "")
            status_str = "FAIL" if level == "critical" else "WARN"
            results.append((f"Alert: {alert_key}", status_str, msg))
    except Exception as exc:
        results.append(("Health report", "FAIL", str(exc)))

    # 3. 持仓管理状态
    try:
        health_resp = _fetch_json(f"{base}/v1/monitoring/health")
        data = health_resp.get("data", {})
        metrics = data.get("latest_metrics", {})

        # reconciliation lag
        recon = metrics.get("position_manager.reconciliation_lag", {})
        if isinstance(recon, dict) and recon.get("value") is not None:
            lag = float(recon["value"])
            if lag > 120:
                results.append(
                    (
                        "Reconciliation lag",
                        "FAIL",
                        f"{lag:.1f}s (> 120s critical)",
                    )
                )
            elif lag > 30:
                results.append(
                    (
                        "Reconciliation lag",
                        "WARN",
                        f"{lag:.1f}s (> 30s warning)",
                    )
                )
            else:
                results.append(("Reconciliation lag", "OK", f"{lag:.1f}s"))

        # circuit breaker
        cb = metrics.get("trade_executor.circuit_breaker_open", {})
        if isinstance(cb, dict) and cb.get("value") is not None:
            is_open = float(cb["value"]) >= 0.5
            if is_open:
                results.append(
                    (
                        "Circuit breaker",
                        "FAIL",
                        "OPEN — auto-trading suspended",
                    )
                )
            else:
                results.append(("Circuit breaker", "OK", "closed"))
    except Exception:
        pass  # health report 已在上面检查

    # 4. 指标引擎（main role: data 含 event_loop_running；executor role: status=disabled 跳过）
    try:
        perf_resp = _fetch_json(f"{base}/v1/monitoring/performance")
        perf_data = perf_resp.get("data", {})
        if not isinstance(perf_data, dict):
            results.append(
                (
                    "Indicator engine",
                    "FAIL",
                    f"unexpected payload type: {type(perf_data).__name__}",
                )
            )
        elif perf_data.get("status") == "disabled":
            # executor role 主动禁用，不算异常，不输出 row
            pass
        elif perf_data.get("event_loop_running"):
            results.append(("Indicator engine", "OK", "event_loop_running"))
        else:
            # main role 但 event loop 未运行（含 fallback {} / 字段缺失）
            results.append(
                (
                    "Indicator engine",
                    "FAIL",
                    f"event_loop_running={perf_data.get('event_loop_running')!r}",
                )
            )
    except (HTTPError, URLError, ValueError) as exc:
        # 异常分层（同 ADR-011）：网络/JSON → FAIL with detail；
        # coding error（AttributeError/TypeError）透传 fail-fast
        results.append(("Indicator engine", "FAIL", str(exc)))

    # 5. 交易模块（trade_executor_summary schema：顶层 enabled / circuit_open /
    # consecutive_failures / execution_count；circuit_open=True 时即使 enabled
    # 也必须告警，避免熔断打开但健康检查仍报 OK 的隐患）
    try:
        trade_resp = _fetch_json(f"{base}/v1/trade/control")
        trade_data = trade_resp.get("data", {})
        executor = trade_data.get("executor", {})
        enabled = executor.get("enabled", False)
        circuit_open = executor.get("circuit_open", False)
        consecutive = executor.get("consecutive_failures", 0)
        exec_count = executor.get("execution_count", 0)
        if circuit_open:
            results.append(
                (
                    "Trade executor",
                    "FAIL",
                    f"circuit OPEN ({consecutive} consecutive failures)",
                )
            )
        elif enabled:
            results.append(
                ("Trade executor", "OK", f"enabled, executions={exec_count}")
            )
        else:
            results.append(("Trade executor", "WARN", "disabled (auto_trade=false)"))
    except (HTTPError, URLError, ValueError) as exc:
        # 异常分层（同 ADR-011）：网络/JSON → WARN with detail；
        # coding error 透传 fail-fast
        results.append(("Trade executor", "WARN", f"unavailable: {exc}"))

    return results


def _render(results: list[tuple[str, str, str]]) -> str:
    lines = ["", "=" * 60, " HEALTH CHECK REPORT", "=" * 60, ""]
    for name, status, detail in results:
        icon = {"OK": "+", "WARN": "!", "FAIL": "X", "DIFF": "~"}.get(status, "?")
        lines.append(f"  [{icon}] {name}: {status}")
        if detail:
            lines.append(f"      {detail}")
    lines.append("")

    ok = sum(1 for _, s, _ in results if s == "OK")
    warn = sum(1 for _, s, _ in results if s == "WARN")
    fail = sum(1 for _, s, _ in results if s == "FAIL")
    lines.append("-" * 60)
    lines.append(f"  OK={ok}  WARN={warn}  FAIL={fail}")

    if fail > 0:
        lines.append("  [X] UNHEALTHY: critical issues detected")
    elif warn > 0:
        lines.append("  [!] DEGRADED: warnings to review")
    else:
        lines.append("  [+] ALL HEALTHY")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post-startup health check for a running MT5Services instance"
    )
    parser.add_argument("--instance", help="实例名（从 topology 配置读取地址）")
    parser.add_argument("--host", default="127.0.0.1", help="API host")
    parser.add_argument("--port", type=int, default=8808, help="API port")
    parser.add_argument(
        "--wait",
        type=float,
        default=0,
        help="等待实例就绪的超时秒数（0=不等待）",
    )
    args = parser.parse_args()
    host, port = _api_target(args)

    if args.wait > 0:
        sys.stderr.write(f"Waiting for {host}:{port} to become ready...\n")
        if not _wait_for_ready(host, port, args.wait):
            print(_render([("Ready probe", "FAIL", "timeout")]))
            raise SystemExit(1)

    sys.stderr.write(f"Checking {host}:{port}...\n")
    results = _run_checks(host, port)
    print(_render(results))

    if any(s == "FAIL" for _, s, _ in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
