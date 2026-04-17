"""Query command handlers — pure read functions over ``RuntimeReadModel``.

Each handler returns a Markdown-ready string (already escaped for Telegram
legacy Markdown). Handlers are deliberately **short, flat, and defensive**:
- No network I/O, no mutation, no exceptions bubbling up.
- If a readmodel method raises or returns an unexpected shape, the handler
  degrades to ``"（<field>: N/A）"`` rather than killing the router.
- Output is kept short (<20 lines) — Telegram is not a dashboard; it's a
  glance surface. Users can drill into the full JSON via HTTP API if needed.

Phase 4 will add ``ControlHandlers`` that call ``OperatorCommandService``
and go through the same router.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from src.notifications.daily_report import DailyReportGenerator
from src.notifications.templates.renderer import escape_markdown

logger = logging.getLogger(__name__)


def _safe_call(fn: Optional[Any]) -> Any:
    if fn is None:
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001 — handlers must never propagate
        logger.exception("handler readmodel call failed")
        return None


def _as_dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _escape(value: Any) -> str:
    """Escape a dynamic value for safe embedding in Markdown reply."""
    return escape_markdown(str(value))


def _yesno(value: Any) -> str:
    if isinstance(value, bool):
        return "✅" if value else "❌"
    if value is None:
        return "—"
    return _escape(value)


def _fmt_count(value: Any) -> str:
    if value is None:
        return "0"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return _escape(value)


class QueryHandlers:
    """Read-only handlers bound to a RuntimeReadModel.

    Each ``handle_*`` method returns the reply text (Markdown). The router
    is responsible for sending it back.
    """

    def __init__(
        self,
        *,
        runtime_read_model: Optional[Any],
        instance: str,
        daily_report_submit: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._rrm = runtime_read_model
        self._instance = instance
        self._daily_report_submit = daily_report_submit

    # ── commands ──

    def handle_help(self) -> str:
        lines = [
            "*可用命令*",
            "",
            "`/health` — 系统健康摘要",
            "`/positions` — 当前持仓一览",
            "`/signals` — 信号引擎状态",
            "`/executor` — 执行器状态 + 熔断",
            "`/pending` — 待执行信号",
            "`/mode` — 运行模式",
            "`/daily-report` — 立即推送日报",
            "`/help` — 显示本帮助",
        ]
        return "\n".join(lines)

    def handle_health(self) -> str:
        report = _as_dict(_safe_call(getattr(self._rrm, "health_report", None)))
        if not report:
            return "_健康读模型不可用_"
        status = str(report.get("status") or report.get("state") or "unknown")
        alerts = report.get("active_alerts") or []
        alert_lines: list[str] = []
        if isinstance(alerts, list) and alerts:
            for alert in alerts[:3]:
                if not isinstance(alert, Mapping):
                    continue
                component = alert.get("component") or "-"
                metric = alert.get("metric_name") or "-"
                level = alert.get("alert_level") or alert.get("severity") or "-"
                alert_lines.append(
                    f"• {_escape(component)}.{_escape(metric)} " f"[{_escape(level)}]"
                )
        lines = [
            "*健康摘要*",
            f"• 实例: {_escape(self._instance)}",
            f"• 状态: {_escape(status)}",
            f"• 活跃告警: {_fmt_count(len(alerts) if isinstance(alerts, list) else 0)}",
        ]
        if alert_lines:
            lines.append("")
            lines.append("_Top 告警_")
            lines.extend(alert_lines)
        return "\n".join(lines)

    def handle_positions(self) -> str:
        payload = _as_dict(
            _safe_call(getattr(self._rrm, "tracked_positions_payload", None))
        )
        if not payload:
            return "_持仓读模型不可用_"
        count = payload.get("count")
        items = payload.get("items") or []
        lines = [
            "*持仓一览*",
            f"• 总数: {_fmt_count(count)}",
        ]
        if not items:
            lines.append("• _无持仓_")
            return "\n".join(lines)
        lines.append("")
        for item in items[:10]:
            if not isinstance(item, Mapping):
                continue
            symbol = item.get("symbol") or "-"
            direction = item.get("direction") or item.get("side") or "-"
            volume = item.get("volume")
            pnl = item.get("unrealized_pnl") or item.get("profit")
            lines.append(
                f"• `{_escape(symbol)}` {_escape(direction)}"
                + (f" x{_escape(volume)}" if volume is not None else "")
                + (f" · pnl={_escape(pnl)}" if pnl is not None else "")
            )
        if isinstance(items, list) and len(items) > 10:
            lines.append(f"_… 省略 {len(items) - 10} 条_")
        return "\n".join(lines)

    def handle_signals(self) -> str:
        summary = _as_dict(
            _safe_call(getattr(self._rrm, "signal_runtime_summary", None))
        )
        if not summary:
            return "_信号读模型不可用_"
        running = summary.get("running")
        queues = _as_dict(summary.get("queues"))
        processing = _as_dict(summary.get("processing"))
        lines = [
            "*信号引擎状态*",
            f"• 运行中: {_yesno(running)}",
        ]
        if queues:
            confirmed_depth = queues.get("confirmed_depth") or queues.get("confirmed")
            intrabar_depth = queues.get("intrabar_depth") or queues.get("intrabar")
            if confirmed_depth is not None:
                lines.append(f"• confirmed 队列深度: {_fmt_count(confirmed_depth)}")
            if intrabar_depth is not None:
                lines.append(f"• intrabar 队列深度: {_fmt_count(intrabar_depth)}")
        if processing:
            drops_1m = processing.get("drop_rate_1m")
            if drops_1m is not None:
                lines.append(f"• 1m 丢弃率: {_escape(drops_1m)}")
        return "\n".join(lines)

    def handle_executor(self) -> str:
        summary = _as_dict(
            _safe_call(getattr(self._rrm, "trade_executor_summary", None))
        )
        if not summary:
            return "_执行器读模型不可用_"
        enabled = summary.get("enabled")
        circuit = summary.get("circuit_open")
        exec_count = summary.get("execution_count")
        signals = _as_dict(summary.get("signals"))
        received = signals.get("received") if signals else None
        passed = signals.get("passed") if signals else None
        last_err = summary.get("last_error")
        lines = [
            "*执行器状态*",
            f"• 启用: {_yesno(enabled)}",
            f"• 熔断: {_yesno(circuit)}",
            f"• 今日下单: {_fmt_count(exec_count)}",
        ]
        if received is not None or passed is not None:
            lines.append(
                f"• 信号 收到/通过: {_fmt_count(received)} / {_fmt_count(passed)}"
            )
        if last_err:
            lines.append(f"• 最近错误: {_escape(str(last_err)[:80])}")
        return "\n".join(lines)

    def handle_pending(self) -> str:
        summary = _as_dict(
            _safe_call(getattr(self._rrm, "pending_entries_summary", None))
        )
        if not summary:
            return "_挂单读模型不可用_"
        count = summary.get("active_count") or summary.get("count")
        entries = summary.get("entries") or []
        lines = [
            "*待执行信号*",
            f"• 总数: {_fmt_count(count)}",
        ]
        if not entries:
            lines.append("• _无_")
            return "\n".join(lines)
        lines.append("")
        for entry in entries[:10]:
            if not isinstance(entry, Mapping):
                continue
            signal_id = entry.get("signal_id") or "-"
            strategy = entry.get("strategy") or "-"
            direction = entry.get("direction") or "-"
            lines.append(
                f"• `{_escape(strategy)}` {_escape(direction)} "
                f"(sig `{_escape(str(signal_id)[:16])}`)"
            )
        return "\n".join(lines)

    def handle_mode(self) -> str:
        summary = _as_dict(_safe_call(getattr(self._rrm, "runtime_mode_summary", None)))
        if not summary:
            return "_模式读模型不可用_"
        current = summary.get("current_mode")
        configured = summary.get("configured_mode")
        last_trans = summary.get("last_transition_at")
        last_reason = summary.get("last_transition_reason")
        lines = [
            "*运行模式*",
            f"• 当前: {_escape(current)}",
        ]
        if configured and configured != current:
            lines.append(f"• 配置: {_escape(configured)}")
        if last_trans:
            lines.append(f"• 上次切换: {_escape(last_trans)}")
        if last_reason:
            lines.append(f"• 原因: {_escape(str(last_reason)[:80])}")
        return "\n".join(lines)

    def handle_daily_report(self) -> str:
        """Manually trigger a daily_report event submission.

        The scheduler normally fires this once per day; this command lets
        operators force one on demand (e.g. after a config change) without
        waiting. Submission goes through the regular dispatcher, so dedup
        may suppress it if already sent today.
        """
        if self._daily_report_submit is None:
            return "_daily_report 未接入（缺少 runtime_read_model）_"
        try:
            submitted = self._daily_report_submit()
        except Exception:  # noqa: BLE001
            logger.exception("manual daily_report submit failed")
            return "❌ 触发失败（见服务端日志）"
        if submitted:
            return "✅ 日报已入队（稍后送达）"
        return "⚠ 日报未入队（可能被去重 / 限流 / 渲染失败）"
