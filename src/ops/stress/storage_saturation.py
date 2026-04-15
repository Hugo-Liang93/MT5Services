"""StorageWriter 队列饱和度采样器。

回答：
  - 8 通道实际 utilization 时序分布（p50/p95/peak）
  - 采样期内是否发生 drops_oldest / drops_newest / blocked_puts / full_errors
  - 哪个通道率先进入 "high"/"critical"/"full" 状态（即饱和瓶颈）

数据源：轮询运行中的实例 HTTP `/v1/monitoring/queues` 端点，按间隔采样。
  - 不向队列注入流量，不模拟负载（那是另一个专门脚本的范围）
  - 此脚本衡量**真实运行时**队列压力，用于判断是否需要扩容、改 overflow 策略、
    或在高频 tick 期提升 batch flush 节奏。

使用前置条件：
  - 目标实例必须运行中（main 或 worker 任一）
  - 默认假设本机 http://127.0.0.1:8808；通过 --host/--port 指定

示例：
    python -m src.ops.stress.storage_saturation --duration 60 --interval 1
    python -m src.ops.stress.storage_saturation --duration 600 --interval 5 --json
    python -m src.ops.stress.storage_saturation --host 127.0.0.1 --port 8809 --duration 120
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


def _fetch_queues(url: str, timeout: float) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    # ApiResponse 包装：{"success": ..., "data": {...}}
    if isinstance(data, dict) and "data" in data:
        payload = data["data"]
    else:
        payload = data
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected response shape: {type(payload).__name__}")
    return payload


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return ordered[k]


def run(
    *,
    host: str,
    port: int,
    duration_seconds: int,
    interval_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    url = f"http://{host}:{port}/v1/monitoring/queues"
    deadline = time.monotonic() + max(1, duration_seconds)

    utilization_series: dict[str, list[float]] = defaultdict(list)
    status_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    first_drops: dict[str, dict[str, int]] = {}
    last_drops: dict[str, dict[str, int]] = {}
    sample_count = 0
    fetch_errors = 0

    while time.monotonic() < deadline:
        start = time.monotonic()
        try:
            snapshot = _fetch_queues(url, timeout_seconds)
        except (URLError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            fetch_errors += 1
            print(f"[warn] fetch failed: {exc}")
        else:
            sample_count += 1
            queues = snapshot.get("queues", {}) or {}
            for name, info in queues.items():
                if not isinstance(info, dict):
                    continue
                util = float(info.get("utilization_pct") or 0.0)
                status = str(info.get("status") or "")
                utilization_series[name].append(util)
                status_counts[name][status] += 1
                drops = {
                    "drops_oldest": int(info.get("drops_oldest") or 0),
                    "drops_newest": int(info.get("drops_newest") or 0),
                    "blocked_puts": int(info.get("blocked_puts") or 0),
                    "full_errors": int(info.get("full_errors") or 0),
                }
                first_drops.setdefault(name, dict(drops))
                last_drops[name] = dict(drops)

        elapsed = time.monotonic() - start
        sleep_for = max(0.0, interval_seconds - elapsed)
        if sleep_for > 0:
            time.sleep(sleep_for)

    # 聚合
    channels = []
    for name, series in sorted(utilization_series.items()):
        first = first_drops.get(name, {})
        last = last_drops.get(name, {})
        delta = {k: last.get(k, 0) - first.get(k, 0) for k in ("drops_oldest", "drops_newest", "blocked_puts", "full_errors")}
        channels.append(
            {
                "name": name,
                "samples": len(series),
                "util_p50_pct": round(_percentile(series, 50), 1),
                "util_p95_pct": round(_percentile(series, 95), 1),
                "util_peak_pct": round(max(series) if series else 0.0, 1),
                "status_counts": dict(status_counts.get(name, {})),
                "drops_delta": delta,
            }
        )

    return {
        "endpoint": url,
        "duration_seconds": duration_seconds,
        "interval_seconds": interval_seconds,
        "samples_collected": sample_count,
        "fetch_errors": fetch_errors,
        "channels": channels,
    }


def _print_text(report: dict[str, Any]) -> None:
    print(f"Endpoint:       {report['endpoint']}")
    print(f"Duration:       {report['duration_seconds']}s @ every {report['interval_seconds']}s")
    print(f"Samples:        {report['samples_collected']} ({report['fetch_errors']} errors)")
    print()
    header = (
        f"{'channel':<28} {'p50':>6} {'p95':>6} {'peak':>6} "
        f"{'oldest':>8} {'newest':>8} {'blocked':>8} {'full':>6}"
    )
    print(header)
    print("-" * len(header))
    for ch in report["channels"]:
        d = ch["drops_delta"]
        print(
            f"{ch['name']:<28} "
            f"{ch['util_p50_pct']:>5}% "
            f"{ch['util_p95_pct']:>5}% "
            f"{ch['util_peak_pct']:>5}% "
            f"{d['drops_oldest']:>8} "
            f"{d['drops_newest']:>8} "
            f"{d['blocked_puts']:>8} "
            f"{d['full_errors']:>6}"
        )
    # 高亮异常
    print()
    hotspots = [ch for ch in report["channels"] if ch["util_peak_pct"] >= 80.0 or any(ch["drops_delta"].values())]
    if hotspots:
        print("饱和/丢失热点:")
        for ch in hotspots:
            print(f"  - {ch['name']}: peak={ch['util_peak_pct']}%, drops={ch['drops_delta']}")
    else:
        print("本次采样期无饱和/丢失事件。")


def main() -> None:
    parser = argparse.ArgumentParser(description="StorageWriter queue saturation sampler")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8808)
    parser.add_argument("--duration", type=int, default=60, help="采样总时长（秒）")
    parser.add_argument("--interval", type=float, default=1.0, help="采样间隔（秒）")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = run(
        host=args.host,
        port=args.port,
        duration_seconds=args.duration,
        interval_seconds=args.interval,
        timeout_seconds=args.timeout,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_text(report)


if __name__ == "__main__":
    main()
