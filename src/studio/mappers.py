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


# ── Helper ─────────────────────────────────────────────────────


def resolve_status(
    agent_id: str,
    checks: list[tuple[bool, str, str, str]],
    default_status: str,
    default_task: str,
    metrics: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Evaluate *checks* top-to-bottom; return the first truthy match.

    Each check is ``(condition, status, task, alert_level)``.
    If none match, use *default_status* / *default_task*.
    """
    for condition, status, task, alert_level in checks:
        if condition:
            return build_agent(agent_id, status, task, metrics=metrics, alert_level=alert_level, **kwargs)
    return build_agent(agent_id, default_status, default_task, metrics=metrics, **kwargs)


# ── 1. collector ← BackgroundIngestor ───────────────────────────


def map_collector(queue_stats: dict[str, Any], is_backfilling: bool) -> dict[str, Any]:
    threads = queue_stats.get("threads", {})
    ingest_alive = threads.get("ingest_alive", False)

    if not ingest_alive:
        return build_agent("collector", "disconnected", "采集线程未运行", alert_level="error")

    summary = queue_stats.get("summary", {})
    full_count = summary.get("full", 0)
    critical_count = summary.get("critical", 0)
    metrics: dict[str, Any] = {
        "channels": summary.get("total", 0),
        "full": full_count,
        "critical": critical_count,
    }

    return resolve_status("collector", [
        (is_backfilling, "thinking", "历史数据回补中", "none"),
        (full_count > 0, "blocked", "队列满溢", "error"),
        (critical_count > 0, "warning", "队列压力高", "warning"),
    ], "working", "实时采集中", metrics=metrics)


# ── 2a. analyst ← UnifiedIndicatorManager (confirmed) ──────────


def map_analyst(perf_stats: dict[str, Any]) -> dict[str, Any]:
    running = perf_stats.get("event_loop_running", False)
    if not running:
        return build_agent("analyst", "error", "指标引擎未运行", alert_level="error")

    scope_stats = perf_stats.get("scope_stats", {})
    confirmed = scope_stats.get("confirmed", {})
    reconcile = scope_stats.get("reconcile", {})
    success_rate = perf_stats.get("success_rate", 0.0)
    computations = confirmed.get("computations", 0)

    metrics: dict[str, Any] = {
        "success_rate": round(float(success_rate), 1),
        "computations": computations,
        "indicator_count": confirmed.get("indicators", 0),
        "reconcile_computations": reconcile.get("computations", 0),
        "indicator_names": perf_stats.get("confirmed_indicators", []),
    }

    no_data = computations == 0 and reconcile.get("computations", 0) == 0
    return resolve_status("analyst", [
        (no_data, "thinking", "等待K线收盘", "none"),
        (success_rate < 80.0 and computations > 0, "warning", "计算失败率偏高", "warning"),
    ], "working", "指标计算中", metrics=metrics)


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

    return resolve_status("live_analyst", [
        (intrabar.get("computations", 0) == 0, "thinking", "等待盘中数据", "none"),
    ], "working", "盘中指标计算中", metrics=metrics)


# ── 3a. strategist ← SignalModule ───────────────────────────────


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

    return resolve_status("strategist", [
        (strategy_count == 0, "error", "无已注册策略", "error"),
        (not recent_signals, "idle", "等待快照", "none"),
    ], "working", "策略评估中", metrics=metrics)


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

    return resolve_status("live_strategist", [
        (not intrabar_strategies, "idle", "无盘中策略", "none"),
        (active_preview > 0, "thinking", "盘中预览中", "none"),
        (bool(preview_signals), "working", "盘中策略评估中", "none"),
    ], "idle", "等待盘中快照", metrics=metrics)


# ── 3c. filter_guard ← SignalRuntime (FilterChain) ───────────


def map_filter_guard(runtime_status: dict[str, Any]) -> dict[str, Any]:
    by_scope = runtime_status.get("filter_by_scope", {})
    confirmed = by_scope.get("confirmed", {})
    intrabar = by_scope.get("intrabar", {})
    total_passed = confirmed.get("passed", 0) + intrabar.get("passed", 0)
    total_blocked = confirmed.get("blocked", 0) + intrabar.get("blocked", 0)

    w_elapsed = runtime_status.get("filter_window_elapsed", 0)
    w_seconds = runtime_status.get("filter_window_seconds", 3600)
    w_by_scope = runtime_status.get("filter_window_by_scope", {})

    filter_total = total_passed + total_blocked
    pass_rate = (total_passed / filter_total * 100) if filter_total > 0 else 0.0

    metrics: dict[str, Any] = {
        "confirmed_passed": confirmed.get("passed", 0),
        "confirmed_blocked": confirmed.get("blocked", 0),
        "confirmed_blocks": confirmed.get("blocks", {}),
        "intrabar_passed": intrabar.get("passed", 0),
        "intrabar_blocked": intrabar.get("blocked", 0),
        "intrabar_blocks": intrabar.get("blocks", {}),
        "total_passed": total_passed,
        "total_blocked": total_blocked,
        "total_snapshots": filter_total,
        "pass_rate": round(pass_rate, 1),
        "window_elapsed": w_elapsed,
        "window_seconds": w_seconds,
        "window_by_scope": w_by_scope,
    }

    if filter_total == 0:
        return build_agent("filter_guard", "idle", "等待指标快照", metrics=metrics)

    return resolve_status("filter_guard", [
        (total_blocked > 0 and pass_rate < 30, "blocked", "环境过滤中", "warning"),
        (total_blocked > 0, "reviewing", "环境过滤中", "none"),
    ], "approved", "环境过滤中", metrics=metrics)


# ── 3d. regime_guard ← SignalRuntime (RegimeDetector + affinity) ─


def map_regime_guard(runtime_status: dict[str, Any]) -> dict[str, Any]:
    affinity_skipped = runtime_status.get("affinity_gates_skipped", 0)
    regime_map: dict[str, dict[str, Any]] = runtime_status.get("regime_map", {})
    per_tf_skips: dict[str, dict[str, int]] = runtime_status.get("per_tf_skips", {})

    # 汇总各 (symbol/tf) 的当前 regime 分布
    regime_counts: dict[str, int] = {}
    for _key, info in regime_map.items():
        r = info.get("current_regime")
        if r:
            regime_counts[r] = regime_counts.get(r, 0) + 1

    metrics: dict[str, Any] = {
        "affinity_skipped": affinity_skipped,
        "per_tf_skips": per_tf_skips,
        "regime_distribution": regime_counts,
        "regime_details": regime_map,
    }

    if not regime_map:
        return build_agent("regime_guard", "idle", "等待 Regime 数据", metrics=metrics)

    return resolve_status("regime_guard", [
        (affinity_skipped > 0, "reviewing", "Regime 研判中", "none"),
    ], "approved", "Regime 研判中", metrics=metrics)


# ── 4. voter ← SignalRuntime ──────────────────────────────────


def map_voter(runtime_status: dict[str, Any]) -> dict[str, Any]:
    running = runtime_status.get("running", False)
    if not running:
        return build_agent("voter", "disconnected", "运行时未启动", alert_level="error")

    voting_groups: list[dict[str, Any]] = runtime_status.get("voting_groups", [])

    metrics: dict[str, Any] = {
        "processed_events": runtime_status.get("processed_events", 0),
        "dropped_events": runtime_status.get("dropped_events", 0),
        "active_confirmed": runtime_status.get("active_confirmed_states", 0),
        "active_preview": runtime_status.get("active_preview_states", 0),
        "voting_groups": voting_groups,
    }

    return resolve_status("voter", [
        (metrics["active_confirmed"] > 0, "working", "投票进行中", "none"),
        (metrics["active_preview"] > 0, "thinking", "预览评估中", "none"),
    ], "idle", "等待策略信号", metrics=metrics)


# ── 5. risk_officer ← TradeExecutor ────────────────────────────


def map_risk_officer(
    executor_status: dict[str, Any],
    support_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    received = executor_status.get("signals_received", 0)
    blocked = executor_status.get("signals_blocked", 0)
    risk_blocks = executor_status.get("execution_quality", {}).get("risk_blocks", 0)
    evidence = support_evidence or {}

    metrics: dict[str, Any] = {
        "signals_received": received,
        "signals_passed": executor_status.get("signals_passed", 0),
        "signals_blocked": blocked,
        "skip_reasons": executor_status.get("skip_reasons", {}),
        "risk_blocks": risk_blocks,
        "by_timeframe": executor_status.get("by_timeframe", {}),
        "support_evidence": evidence,
        "upstream_modules": [key for key in evidence.keys()],
    }

    return resolve_status("risk_officer", [
        (risk_blocks > 0, "blocked", "风控拦截", "warning"),
        (blocked > 0, "reviewing", "风控审核中", "none"),
        (received > 0, "approved", "风控通过", "none"),
    ], "idle", "等待信号", metrics=metrics)


# ── 6. trader ← TradeExecutor ─────────────────────────────────


def map_trader(
    executor_status: dict[str, Any],
    pending_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    enabled = executor_status.get("enabled", False)
    circuit = executor_status.get("circuit_breaker", {})
    circuit_open = circuit.get("open", False)
    last_error = executor_status.get("last_error")
    last_risk_block = executor_status.get("last_risk_block")
    recent = executor_status.get("recent_executions", [])

    pe = pending_status or {}
    pe_entries = pe.get("entries", [])
    pe_stats = pe.get("stats", {})

    metrics: dict[str, Any] = {
        "enabled": enabled,
        "execution_count": executor_status.get("execution_count", 0),
        "circuit_open": circuit_open,
        "consecutive_failures": circuit.get("consecutive_failures", 0),
        "last_error": last_error[:80] if last_error else None,
        "last_risk_block": last_risk_block[:80] if last_risk_block else None,
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
        return build_agent("trader", "blocked", "熔断器触发", metrics=metrics, alert_level="error")
    if last_error:
        return build_agent("trader", "warning", "执行异常", metrics=metrics, alert_level="warning")
    if last_risk_block:
        return build_agent("trader", "idle", "风控拦截", metrics=metrics)
    if pe_entries:
        pe_symbol = pe_entries[0].get("symbol", "") if pe_entries else ""
        return build_agent("trader", "waiting", "价格确认中", metrics=metrics, symbol=pe_symbol)
    if recent:
        return build_agent("trader", "working", "交易执行中", metrics=metrics, symbol=recent[-1].get("symbol", ""))
    return build_agent("trader", "idle", "等待信号", metrics=metrics)


# ── 7. position_manager ← PositionManager ─────────────────────


def map_position_manager(
    positions: list[dict[str, Any]],
    pm_status: dict[str, Any],
) -> dict[str, Any]:
    running = pm_status.get("running", False)
    if not running:
        return build_agent("position_manager", "disconnected", "持仓管理器未运行", alert_level="error")

    total_pnl = sum(
        float(
            p.get("unrealized_pnl", p.get("profit", 0)) or 0
        )
        for p in positions
    )
    metrics: dict[str, Any] = {
        "tracked_positions": pm_status.get("tracked_positions", 0),
        "reconcile_count": pm_status.get("reconcile_count", 0),
        "total_pnl": round(total_pnl, 2),
    }

    if not positions:
        return build_agent("position_manager", "idle", "无活跃持仓", metrics=metrics)

    alert = "warning" if total_pnl < 0 else "none"
    return build_agent("position_manager", "working", "持仓监控中", metrics=metrics, alert_level=alert)


# ── 8. accountant ← TradingModule ─────────────────────────────


def map_accountant(
    account_info: dict[str, Any],
    trade_control: dict[str, Any],
    margin_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    equity = account_info.get("equity", 0)
    margin = account_info.get("margin", 0)
    margin_pct = (equity / margin * 100) if margin > 0 else float("inf")
    close_only = trade_control.get("close_only_mode", False)
    auto_entry = trade_control.get("auto_entry_enabled", True)

    mg = margin_guard or {}
    mg_state = mg.get("state", "unknown")

    metrics: dict[str, Any] = {
        "balance": account_info.get("balance", 0),
        "equity": equity,
        "margin": margin,
        "free_margin": account_info.get("free_margin") or account_info.get("margin_free", 0),
        "margin_pct": round(margin_pct, 1) if margin > 0 else None,
        "auto_entry": auto_entry,
        "close_only": close_only,
        "margin_guard": mg if mg else None,
    }

    return resolve_status("accountant", [
        (close_only, "warning", "仅平仓模式", "warning"),
        (mg_state == "critical", "error", "保证金紧急", "error"),
        (mg_state == "danger", "alert", "保证金危险", "error"),
        (mg_state == "warn", "warning", "保证金预警", "warning"),
        (margin_pct < 150, "alert", "保证金不足", "error"),
        (not auto_entry, "idle", "自动入场已关闭", "info"),
    ], "working", "账务正常", metrics=metrics)


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
        return build_agent("calendar_reporter", "disconnected", "日历服务未运行", metrics=metrics, alert_level="error")

    return resolve_status("calendar_reporter", [
        (stale, "alert", "日历数据过期", "error"),
        (bool(high_impact), "warning", "高影响事件激活", "warning"),
        (failures > 0, "warning", "数据拉取异常", "warning"),
    ], "working", "日历监控中", metrics=metrics)


# ── 10. inspector ← HealthMonitor ─────────────────────────────


def map_backtester(backtest_status: dict[str, Any]) -> dict[str, Any]:
    running_jobs = int(backtest_status.get("running_jobs", 0) or 0)
    pending_jobs = int(backtest_status.get("pending_jobs", 0) or 0)
    failed_jobs = int(backtest_status.get("failed_jobs", 0) or 0)
    completed_jobs = int(backtest_status.get("completed_jobs", 0) or 0)

    metrics: dict[str, Any] = {
        "running_jobs": running_jobs,
        "pending_jobs": pending_jobs,
        "completed_jobs": completed_jobs,
        "failed_jobs": failed_jobs,
        "latest_job": backtest_status.get("latest_job"),
        "result_cache_size": backtest_status.get("result_cache_size", 0),
    }

    return resolve_status("backtester", [
        (running_jobs > 0, "working", "回测任务运行中", "none"),
        (pending_jobs > 0, "thinking", "回测任务排队中", "none"),
        (failed_jobs > 0, "warning", "最近回测存在失败", "warning"),
        (completed_jobs > 0, "reviewing", "研究结果可复核", "none"),
    ], "idle", "等待研究任务", metrics=metrics)


def map_inspector(health_report: dict[str, Any]) -> dict[str, Any]:
    overall = health_report.get("overall_status", "unknown")
    metrics: dict[str, Any] = {
        "overall_status": overall,
        "active_alert_count": len(health_report.get("active_alerts", [])),
    }

    return resolve_status("inspector", [
        (overall == "critical", "error", "系统异常", "error"),
        (overall == "warning", "alert", "系统警告", "warning"),
    ], "reviewing", "系统健康", metrics=metrics)
