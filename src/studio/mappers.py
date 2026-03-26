"""Pure mapping functions: module status dicts → StudioAgent dicts.

Each function accepts only primitive data (dict, bool, list, etc.),
never module instances.  This ensures Studio is fully decoupled from
business modules — the wiring between modules and mappers lives
exclusively in ``builder.py``.

All functions are stateless and safe to call from any thread.

Design principle — backend returns DATA, frontend renders DISPLAY:
    - ``status``:     semantic enum (idle/working/error/...) — drives animation & color
    - ``alertLevel``: severity enum (none/info/warning/error) — drives alert badge
    - ``task``:       brief status phrase, NO embedded numbers/metrics
    - ``metrics``:    structured key-value data — frontend reads this for all display
"""

from __future__ import annotations

from typing import Any

from .models import build_agent


# ── 1. collector ← BackgroundIngestor ───────────────────────────


def map_collector(queue_stats: dict[str, Any], is_backfilling: bool) -> dict[str, Any]:
    threads = queue_stats.get("threads", {})
    ingest_alive = threads.get("ingest_alive", False)

    if not ingest_alive:
        return build_agent("collector", "disconnected", "采集线程未运行", alert_level="error")
    if is_backfilling:
        return build_agent("collector", "thinking", "历史数据回补中")

    summary = queue_stats.get("summary", {})
    full_count = summary.get("full", 0)
    critical_count = summary.get("critical", 0)

    metrics: dict[str, Any] = {
        "channels": summary.get("total", 0),
        "full": full_count,
        "critical": critical_count,
    }

    if full_count > 0:
        return build_agent(
            "collector", "blocked", "队列满溢",
            metrics=metrics, alert_level="error",
        )
    if critical_count > 0:
        return build_agent(
            "collector", "warning", "队列压力高",
            metrics=metrics, alert_level="warning",
        )
    return build_agent("collector", "working", "实时采集中", metrics=metrics)


# ── 2a. analyst ← UnifiedIndicatorManager (confirmed) ──────────


def map_analyst(perf_stats: dict[str, Any]) -> dict[str, Any]:
    running = perf_stats.get("event_loop_running", False)

    if not running:
        return build_agent("analyst", "error", "指标引擎未运行", alert_level="error")

    scope_stats = perf_stats.get("scope_stats", {})
    confirmed = scope_stats.get("confirmed", {})
    reconcile = scope_stats.get("reconcile", {})
    success_rate = perf_stats.get("success_rate", 0.0)
    pct = success_rate * 100 if success_rate <= 1.0 else success_rate

    metrics: dict[str, Any] = {
        "success_rate": round(pct, 1),
        "computations": confirmed.get("computations", 0),
        "indicator_count": confirmed.get("indicators", 0),
        "reconcile_computations": reconcile.get("computations", 0),
        "indicator_names": perf_stats.get("confirmed_indicators", []),
    }

    if confirmed.get("computations", 0) == 0 and reconcile.get("computations", 0) == 0:
        return build_agent("analyst", "thinking", "等待K线收盘", metrics=metrics)
    return build_agent("analyst", "working", "指标计算中", metrics=metrics)


# ── 2b. live_analyst ← UnifiedIndicatorManager (intrabar) ──────


def map_live_analyst(
    perf_stats: dict[str, Any],
    intrabar_bars_by_tf: dict[str, list[Any]] | None = None,
    intrabar_ingest_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    running = perf_stats.get("event_loop_running", False)

    if not running:
        return build_agent("live_analyst", "error", "指标引擎未运行", alert_level="error")

    scope_stats = perf_stats.get("scope_stats", {})
    intrabar = scope_stats.get("intrabar", {})
    ing = intrabar_ingest_stats or {}

    # 按 TF 的 intrabar bar OHLC 快照（前端画迷你 K 线）
    bars_by_tf: dict[str, dict[str, Any]] = {}
    for tf, bars in (intrabar_bars_by_tf or {}).items():
        if not bars:
            continue
        bar_time = getattr(bars[0], "time", None)
        bars_by_tf[tf] = {
            "bar_time": bar_time.isoformat() if bar_time else None,
            "snapshots": [
                {
                    "o": float(getattr(b, "open", 0)),
                    "h": float(getattr(b, "high", 0)),
                    "l": float(getattr(b, "low", 0)),
                    "c": float(getattr(b, "close", 0)),
                }
                for b in bars[-30:]
            ],
        }

    metrics: dict[str, Any] = {
        "computations": intrabar.get("computations", 0),
        "indicator_count": intrabar.get("indicators", 0),
        "indicator_names": perf_stats.get("intrabar_indicators", []),
        "bars_by_tf": bars_by_tf,
        "ingest_polls": ing.get("polls", 0),
        "ingest_deduped": ing.get("deduped", 0),
        "ingest_updated": ing.get("updated", 0),
    }

    if intrabar.get("computations", 0) == 0:
        return build_agent("live_analyst", "thinking", "等待盘中数据", metrics=metrics)
    return build_agent("live_analyst", "working", "盘中指标计算中", metrics=metrics)


# ── 3. strategist ← SignalModule ───────────────────────────────


def map_strategist(
    strategy_count: int,
    recent_signals: list[dict[str, Any]],
) -> dict[str, Any]:
    buy = sum(1 for s in recent_signals if s.get("direction") == "buy")
    sell = sum(1 for s in recent_signals if s.get("direction") == "sell")

    metrics: dict[str, Any] = {
        "strategy_count": strategy_count,
        "recent_signal_count": len(recent_signals),
        "buy_count": buy,
        "sell_count": sell,
    }

    if strategy_count == 0:
        return build_agent(
            "strategist", "error", "无已注册策略",
            metrics=metrics, alert_level="error",
        )
    if not recent_signals:
        return build_agent("strategist", "idle", "等待快照", metrics=metrics)
    return build_agent("strategist", "working", "策略评估中", metrics=metrics)


# ── 3b. live_strategist ← SignalModule (intrabar preview/armed) ─


def map_live_strategist(
    intrabar_strategies: list[str],
    preview_signals: list[dict[str, Any]],
    runtime_status: dict[str, Any],
) -> dict[str, Any]:
    buy = sum(1 for s in preview_signals if s.get("direction") == "buy")
    sell = sum(1 for s in preview_signals if s.get("direction") == "sell")
    active_preview = runtime_status.get("active_preview_states", 0)

    metrics: dict[str, Any] = {
        "intrabar_strategy_count": len(intrabar_strategies),
        "intrabar_strategy_names": intrabar_strategies,
        "preview_signal_count": len(preview_signals),
        "buy_count": buy,
        "sell_count": sell,
        "active_preview": active_preview,
    }

    if not intrabar_strategies:
        return build_agent("live_strategist", "idle", "无盘中策略", metrics=metrics)
    if active_preview > 0:
        return build_agent("live_strategist", "thinking", "盘中预览中", metrics=metrics)
    if preview_signals:
        return build_agent("live_strategist", "working", "盘中策略评估中", metrics=metrics)
    return build_agent("live_strategist", "idle", "等待盘中快照", metrics=metrics)


# ── 3b. auditor ← SignalRuntime (FilterChain + affinity) ─────


def map_auditor(runtime_status: dict[str, Any]) -> dict[str, Any]:
    affinity_skipped = runtime_status.get("affinity_gates_skipped", 0)

    # 按 scope 分维度（累计）
    by_scope = runtime_status.get("filter_by_scope", {})
    confirmed = by_scope.get("confirmed", {})
    intrabar = by_scope.get("intrabar", {})
    total_passed = confirmed.get("passed", 0) + intrabar.get("passed", 0)
    total_blocked = confirmed.get("blocked", 0) + intrabar.get("blocked", 0)

    # 滑动窗口（按 scope 分维度）
    w_elapsed = runtime_status.get("filter_window_elapsed", 0)
    w_seconds = runtime_status.get("filter_window_seconds", 3600)
    w_by_scope = runtime_status.get("filter_window_by_scope", {})

    metrics: dict[str, Any] = {
        "affinity_skipped": affinity_skipped,
        # 累计 — 按 scope 分维度
        "confirmed_passed": confirmed.get("passed", 0),
        "confirmed_blocked": confirmed.get("blocked", 0),
        "confirmed_blocks": confirmed.get("blocks", {}),
        "intrabar_passed": intrabar.get("passed", 0),
        "intrabar_blocked": intrabar.get("blocked", 0),
        "intrabar_blocks": intrabar.get("blocks", {}),
        "total_passed": total_passed,
        "total_blocked": total_blocked,
        "total_snapshots": total_passed + total_blocked,
        # 滑动窗口 — 按 scope 分维度
        "window_elapsed": w_elapsed,
        "window_seconds": w_seconds,
        "window_by_scope": w_by_scope,
    }

    total_evaluated = total_passed + total_blocked
    if total_evaluated == 0:
        return build_agent("auditor", "idle", "等待指标快照", metrics=metrics)

    pass_rate = total_passed / total_evaluated * 100

    if total_blocked > 0 and pass_rate < 30:
        return build_agent(
            "auditor", "blocked", "信号审核中",
            metrics=metrics, alert_level="warning",
        )
    if total_blocked > 0:
        return build_agent("auditor", "reviewing", "信号审核中", metrics=metrics)
    return build_agent("auditor", "approved", "信号审核中", metrics=metrics)


# ── 4. voter ← SignalRuntime ──────────────────────────────────


def map_voter(runtime_status: dict[str, Any]) -> dict[str, Any]:
    running = runtime_status.get("running", False)

    if not running:
        return build_agent("voter", "disconnected", "运行时未启动", alert_level="error")

    metrics: dict[str, Any] = {
        "processed_events": runtime_status.get("processed_events", 0),
        "dropped_events": runtime_status.get("dropped_events", 0),
        "active_confirmed": runtime_status.get("active_confirmed_states", 0),
        "active_preview": runtime_status.get("active_preview_states", 0),
    }

    if metrics["active_confirmed"] > 0:
        return build_agent("voter", "working", "投票进行中", metrics=metrics)
    if metrics["active_preview"] > 0:
        return build_agent("voter", "thinking", "预览评估中", metrics=metrics)
    return build_agent("voter", "idle", "等待策略信号", metrics=metrics)


# ── 5. risk_officer ← SignalRuntime + StorageWriter ────────────


def map_risk_officer(
    executor_status: dict[str, Any],
) -> dict[str, Any]:
    received = executor_status.get("signals_received", 0)
    passed = executor_status.get("signals_passed", 0)
    blocked = executor_status.get("signals_blocked", 0)
    skip_reasons = executor_status.get("skip_reasons", {})
    risk_blocks = executor_status.get("execution_quality", {}).get("risk_blocks", 0)

    metrics: dict[str, Any] = {
        "signals_received": received,
        "signals_passed": passed,
        "signals_blocked": blocked,
        "skip_reasons": skip_reasons,
        "risk_blocks": risk_blocks,
    }

    if risk_blocks > 0:
        return build_agent(
            "risk_officer", "blocked", "风控拦截",
            metrics=metrics, alert_level="warning",
        )
    if blocked > 0:
        return build_agent("risk_officer", "reviewing", "风控审核中", metrics=metrics)
    if received > 0:
        return build_agent("risk_officer", "approved", "风控通过", metrics=metrics)
    return build_agent("risk_officer", "idle", "等待信号", metrics=metrics)


# ── 6. trader ← TradeExecutor ─────────────────────────────────


def map_trader(
    executor_status: dict[str, Any],
    pending_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    enabled = executor_status.get("enabled", False)
    circuit = executor_status.get("circuit_breaker", {})
    circuit_open = circuit.get("open", False)
    last_error = executor_status.get("last_error")
    recent = executor_status.get("recent_executions", [])

    # pending entry 数据
    pe = pending_status or {}
    pe_entries = pe.get("entries", [])
    pe_stats = pe.get("stats", {})

    metrics: dict[str, Any] = {
        "enabled": enabled,
        "execution_count": executor_status.get("execution_count", 0),
        "circuit_open": circuit_open,
        "consecutive_failures": circuit.get("consecutive_failures", 0),
        "last_error": last_error[:80] if last_error else None,
        # pending entry
        "pending_entries": pe_entries,
        "pending_count": len(pe_entries),
        "pending_stats": {
            "total_submitted": pe_stats.get("total_submitted", 0),
            "total_filled": pe_stats.get("total_filled", 0),
            "total_expired": pe_stats.get("total_expired", 0),
            "fill_rate": pe_stats.get("fill_rate"),
            "avg_price_improvement": pe_stats.get("avg_price_improvement"),
        },
    }

    if not enabled:
        return build_agent("trader", "disconnected", "自动交易已禁用", metrics=metrics)
    if circuit_open:
        return build_agent(
            "trader", "blocked", "熔断器触发",
            metrics=metrics, alert_level="error",
        )
    if last_error:
        return build_agent(
            "trader", "warning", "执行异常",
            metrics=metrics, alert_level="warning",
        )
    if pe_entries:
        return build_agent(
            "trader", "waiting", "价格确认中",
            metrics=metrics, symbol="XAUUSD",
        )
    if recent:
        last = recent[-1]
        symbol = last.get("symbol", "XAUUSD")
        return build_agent(
            "trader", "working", "交易执行中",
            metrics=metrics, symbol=symbol,
        )
    return build_agent("trader", "idle", "等待信号", metrics=metrics)


# ── 7. position_manager ← PositionManager ─────────────────────


def map_position_manager(
    positions: list[dict[str, Any]],
    pm_status: dict[str, Any],
) -> dict[str, Any]:
    running = pm_status.get("running", False)
    tracked = pm_status.get("tracked_positions", 0)

    if not running:
        return build_agent(
            "position_manager", "disconnected", "持仓管理器未运行",
            alert_level="error",
        )

    total_pnl = sum(float(p.get("profit", 0) or 0) for p in positions)

    metrics: dict[str, Any] = {
        "tracked_positions": tracked,
        "reconcile_count": pm_status.get("reconcile_count", 0),
        "total_pnl": round(total_pnl, 2),
    }

    if not positions:
        return build_agent("position_manager", "idle", "无活跃持仓", metrics=metrics)

    alert = "warning" if total_pnl < 0 else "none"
    return build_agent(
        "position_manager", "working", "持仓监控中",
        metrics=metrics, alert_level=alert,
    )


# ── 8. accountant ← TradingModule ─────────────────────────────


def map_accountant(
    account_info: dict[str, Any],
    trade_control: dict[str, Any],
) -> dict[str, Any]:
    balance = account_info.get("balance", 0)
    equity = account_info.get("equity", 0)
    margin = account_info.get("margin", 0)
    free_margin = account_info.get("free_margin") or account_info.get("margin_free", 0)
    margin_pct = (equity / margin * 100) if margin > 0 else 9999.0

    auto_entry = trade_control.get("auto_entry_enabled", True)
    close_only = trade_control.get("close_only_mode", False)

    metrics: dict[str, Any] = {
        "balance": balance,
        "equity": equity,
        "margin": margin,
        "free_margin": free_margin,
        "margin_pct": round(margin_pct, 1),
        "auto_entry": auto_entry,
        "close_only": close_only,
    }

    if close_only:
        return build_agent(
            "accountant", "warning", "仅平仓模式",
            metrics=metrics, alert_level="warning",
        )
    if margin_pct < 150:
        return build_agent(
            "accountant", "alert", "保证金不足",
            metrics=metrics, alert_level="error",
        )
    if not auto_entry:
        return build_agent(
            "accountant", "idle", "自动入场已关闭",
            metrics=metrics, alert_level="info",
        )
    return build_agent("accountant", "working", "账务正常", metrics=metrics)


# ── 9. calendar_reporter ← EconomicCalendarService ────────────


def map_calendar_reporter(
    calendar_stats: dict[str, Any],
    risk_windows: list[dict[str, Any]],
) -> dict[str, Any]:
    stale = calendar_stats.get("stale", "false") == "true"
    running = calendar_stats.get("running", "false") == "true"
    failures = int(calendar_stats.get("consecutive_failures", 0))

    high_impact = [
        w for w in risk_windows
        if str(w.get("impact", "")).lower() == "high"
        and w.get("guard_active", False)
    ]

    metrics: dict[str, Any] = {
        "risk_window_count": len(risk_windows),
        "high_impact_active": len(high_impact),
        "consecutive_failures": failures,
        "stale": stale,
    }

    if not running:
        return build_agent(
            "calendar_reporter", "disconnected", "日历服务未运行",
            metrics=metrics, alert_level="error",
        )
    if stale:
        return build_agent(
            "calendar_reporter", "alert", "日历数据过期",
            metrics=metrics, alert_level="error",
        )
    if high_impact:
        return build_agent(
            "calendar_reporter", "warning", "高影响事件激活",
            metrics=metrics, alert_level="warning",
        )
    if failures > 0:
        return build_agent(
            "calendar_reporter", "warning", "数据拉取异常",
            metrics=metrics, alert_level="warning",
        )
    return build_agent("calendar_reporter", "working", "日历监控中", metrics=metrics)


# ── 10. inspector ← HealthMonitor ─────────────────────────────


def map_inspector(health_report: dict[str, Any]) -> dict[str, Any]:
    overall = health_report.get("overall_status", "unknown")
    active_alerts = health_report.get("active_alerts", [])

    metrics: dict[str, Any] = {
        "overall_status": overall,
        "active_alert_count": len(active_alerts),
    }

    if overall == "critical":
        return build_agent(
            "inspector", "error", "系统异常",
            metrics=metrics, alert_level="error",
        )
    if overall == "warning":
        return build_agent(
            "inspector", "alert", "系统警告",
            metrics=metrics, alert_level="warning",
        )
    return build_agent("inspector", "reviewing", "系统健康", metrics=metrics)
