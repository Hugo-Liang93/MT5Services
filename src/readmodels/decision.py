"""Decision brief rendering service for API-facing decision context."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _resolve_stance(context: Mapping[str, Any]) -> str:
    dominant = str(_as_mapping(context.get("signals")).get("dominantBias") or "观望")
    risks = _as_mapping(context.get("risks"))
    operations = _as_mapping(context.get("operations"))
    system = _as_mapping(context.get("system"))

    if (
        _safe_int(risks.get("activeGuardCount")) > 0
        or system.get("healthStatus") == "unhealthy"
        or _safe_int(operations.get("abnormalRoleCount")) >= 3
    ):
        return "观望"
    if dominant in {"偏多", "偏空", "观望"}:
        return dominant
    return "观望"


def _resolve_confidence(context: Mapping[str, Any], stance: str) -> float:
    signals = _as_mapping(context.get("signals"))
    risks = _as_mapping(context.get("risks"))
    operations = _as_mapping(context.get("operations"))
    system = _as_mapping(context.get("system"))

    confidence = 0.42
    confidence += min(_safe_int(signals.get("formalCount")) * 0.07, 0.21)
    confidence += min(_safe_int(signals.get("previewCount")) * 0.03, 0.09)
    confidence -= min(_safe_int(risks.get("activeGuardCount")) * 0.16, 0.32)
    confidence -= min(_safe_int(operations.get("queuePressureCount")) * 0.05, 0.15)
    confidence -= min(_safe_int(operations.get("abnormalRoleCount")) * 0.04, 0.16)

    health = str(system.get("healthStatus") or "unknown")
    if health == "healthy":
        confidence += 0.06
    elif health == "degraded":
        confidence -= 0.05
    elif health == "unhealthy":
        confidence -= 0.15

    if stance == "观望":
        confidence = min(confidence, 0.62)

    return round(max(0.1, min(confidence, 0.95)), 2)


def _build_evidence(context: Mapping[str, Any], stance: str) -> list[dict[str, str]]:
    market = _as_mapping(context.get("market"))
    signals = _as_mapping(context.get("signals"))
    risks = _as_mapping(context.get("risks"))
    operations = _as_mapping(context.get("operations"))

    spread = market.get("spread")
    spread_text = "--"
    spread_tone = "neutral"
    if spread is not None:
        spread_value = _safe_float(spread)
        spread_text = f"{spread_value:.2f}"
        spread_tone = "warning" if spread_value > 0.5 else "positive"

    guard_count = _safe_int(risks.get("activeGuardCount"))
    open_count = _safe_int(market.get("openPositionCount"))
    open_pnl = _safe_float(market.get("openPnl"))

    return [
        {
            "title": "策略倾向",
            "value": f"{_safe_int(signals.get('formalCount'))} 条正式信号 / {stance}",
            "tone": "positive" if stance != "观望" else "neutral",
        },
        {
            "title": "市场点差",
            "value": spread_text,
            "tone": spread_tone,
        },
        {
            "title": "风险保护",
            "value": f"{guard_count} 个保护窗口生效" if guard_count else "当前无保护阻断",
            "tone": "danger" if guard_count else "positive",
        },
        {
            "title": "执行暴露",
            "value": (
                f"{open_count} 笔持仓 / {'+' if open_pnl > 0 else ''}{open_pnl:.2f}"
                if open_count
                else "当前无持仓暴露"
            ),
            "tone": "warning" if open_pnl < 0 else ("positive" if open_count else "neutral"),
        },
        {
            "title": "队列压力",
            "value": (
                f"{_safe_int(operations.get('queuePressureCount'))} 个队列接近上限"
                if _safe_int(operations.get("queuePressureCount"))
                else "当前无队列压力"
            ),
            "tone": "warning" if _safe_int(operations.get("queuePressureCount")) else "positive",
        },
    ]


def _build_conflicts(context: Mapping[str, Any], stance: str) -> list[str]:
    signals = _as_mapping(context.get("signals"))
    risks = _as_mapping(context.get("risks"))
    operations = _as_mapping(context.get("operations"))

    conflicts: list[str] = []
    if _safe_int(signals.get("buyCount")) > 0 and _safe_int(signals.get("sellCount")) > 0:
        conflicts.append("当前多空信号并存，策略共识不足。")
    if _safe_int(risks.get("highImpactWindowCount")) > 0:
        conflicts.append("高影响事件窗口临近，方向判断容易被宏观事件打断。")
    if _safe_int(operations.get("queuePressureCount")) > 0:
        conflicts.append("实时队列存在压力，盘中数据新鲜度需要复核。")
    if stance == "观望" and not conflicts:
        conflicts.append("当前主链路没有形成足够一致的推进条件。")
    return conflicts[:3]


def _build_risks(context: Mapping[str, Any]) -> list[str]:
    risks = _as_mapping(context.get("risks"))
    operations = _as_mapping(context.get("operations"))
    system = _as_mapping(context.get("system"))
    messages = list(risks.get("topWarnings") or [])

    result: list[str] = []
    if _safe_int(risks.get("activeGuardCount")) > 0:
        labels = list(risks.get("activeGuardLabels") or [])
        label_text = "、".join(labels[:2]) if labels else "高影响事件保护"
        result.append(f"风险保护已生效：{label_text}。")
    if _safe_int(operations.get("disconnectedRoleCount")) > 0:
        result.append("存在断连角色，执行前需确认链路完整性。")
    if system.get("healthStatus") == "unhealthy":
        result.append("系统整体健康度异常，不适合扩大交易动作。")
    result.extend(str(item) for item in messages[:2] if str(item).strip())
    return result[:4]


def _build_invalidations(context: Mapping[str, Any], stance: str) -> list[str]:
    market = _as_mapping(context.get("market"))
    risks = _as_mapping(context.get("risks"))
    operations = _as_mapping(context.get("operations"))

    invalidations = [
        "最新报价延迟或点差继续扩大时，本次建议失效。",
        "风控保护窗口新增高影响事件时，应重新评估。",
    ]
    if _safe_int(operations.get("queuePressureCount")) > 0:
        invalidations.append("关键队列压力未解除前，不应放大执行动作。")
    if stance in {"偏多", "偏空"} and _safe_int(market.get("openPositionCount")) > 0:
        invalidations.append("现有持仓方向与新增建议冲突时，应先处理持仓暴露。")
    if _safe_int(risks.get("activeGuardCount")) == 0 and stance == "观望":
        invalidations.append("只有在正式信号数量与方向一致性提升后，观望结论才应解除。")
    return invalidations[:4]


def _resolve_recommended_action(context: Mapping[str, Any], stance: str) -> tuple[str, str]:
    risks = _as_mapping(context.get("risks"))
    operations = _as_mapping(context.get("operations"))
    focus = _as_mapping(context.get("focus"))

    if _safe_int(risks.get("activeGuardCount")) > 0:
        return "等待保护窗口结束", "优先查看风控官与日历模块，确认保护窗口何时解除。"
    if _safe_int(operations.get("queuePressureCount")) > 0:
        return "先恢复链路稳定", "优先查看采集区与分析区，确认实时数据队列压力。"
    if stance == "观望":
        return "继续观察并等待共识", "优先查看策略区与投票主席，等待正式信号形成一致结论。"
    if stance == "偏多":
        return "准备小仓位试单", "先复核风控官与交易员，确认点差和账户约束后再推进执行。"
    focus_roles = list(focus.get("focusRoles") or [])
    if focus_roles:
        return "准备空头执行检查", f"先回看{focus_roles[0]}相关事实层，再决定是否推进执行。"
    return "准备空头执行检查", "先复核风控官与交易员，再决定是否推进执行。"


def _build_summary(context: Mapping[str, Any], stance: str) -> str:
    focus = _as_mapping(context.get("focus"))
    signals = _as_mapping(context.get("signals"))
    risks = _as_mapping(context.get("risks"))
    operations = _as_mapping(context.get("operations"))

    title = str(focus.get("title") or "交易全局")
    formal_count = _safe_int(signals.get("formalCount"))
    guard_count = _safe_int(risks.get("activeGuardCount"))
    abnormal_count = _safe_int(operations.get("abnormalRoleCount"))

    if stance == "观望":
        return (
            f"{title} 当前更适合保持观望。"
            f"正式信号 {formal_count} 条，保护窗口 {guard_count} 个，异常角色 {abnormal_count} 个。"
        )
    if stance == "偏多":
        return (
            f"{title} 当前偏多，但仍需先通过风控与执行条件复核。"
            f"正式信号 {formal_count} 条，当前保护窗口 {guard_count} 个。"
        )
    return (
        f"{title} 当前偏空，但应先确认风险与链路稳定性。"
        f"正式信号 {formal_count} 条，异常角色 {abnormal_count} 个。"
    )


def build_decision_brief(context: Mapping[str, Any]) -> dict[str, Any]:
    focus = _as_mapping(context.get("focus"))
    stance = _resolve_stance(context)
    confidence = _resolve_confidence(context, stance)
    recommended_action, action_hint = _resolve_recommended_action(context, stance)

    generated_at = (
        str(context.get("generatedAt")).strip()
        or datetime.now(timezone.utc).isoformat()
    )

    return {
        "focusTitle": str(focus.get("title") or "交易全局"),
        "focusSubtitle": str(focus.get("subtitle") or "当前关注全链路决策上下文"),
        "stance": stance,
        "confidence": confidence,
        "summary": _build_summary(context, stance),
        "recommendedAction": recommended_action,
        "actionHint": action_hint,
        "evidence": _build_evidence(context, stance)[:4],
        "conflicts": _build_conflicts(context, stance),
        "risks": _build_risks(context),
        "invalidations": _build_invalidations(context, stance),
        "focusRoles": list(focus.get("focusRoles") or []),
        "generatedAt": generated_at,
        "source": "remote",
        "sourceLabel": "后端决策引擎",
    }
