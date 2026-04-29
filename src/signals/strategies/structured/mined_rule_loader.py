"""mined_rule_loader — 从 mining JSON 提取 MinedRuleSpec 并按门禁筛选。

工作流：
    mining_runner --json-output X.json
        ↓
    load_specs_from_path(X) → List[MinedRuleSpec]
        ↓
    filter_promotable(specs) → 通过门禁的子集
        ↓
    catalog 注册 → MinedRuleStrategy 实例
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from .mined_rule import MinedRuleBarrier, MinedRuleCondition, MinedRuleSpec

# Stage 1 — 晋级门禁（mining-time 统计）。审计基于 mining JSON 的 in-sample /
# out-of-sample / cost-after / barrier hit-rate。改阈值需要审计原因。
PROMOTION_GATES: Dict[str, float] = {
    "min_train_wr": 0.55,  # train hit_rate >= 55%
    "min_test_wr": 0.52,  # test hit_rate >= 52%
    "min_test_n": 30,  # test 样本不能太少
    "max_train_test_drop": 0.30,  # (train - test) / train 不超过 30%
    "min_barrier_wr": 0.50,  # 实际 barrier hit_rate >= 50%
    "min_mean_return": 0.0,  # cost-after 正期望
}

# Stage 2 — backtest 实测门禁。mining 统计与 BacktestEngine 实测可显著背离
# （filter chain / position management / time_exit / breakeven / signal_exit
# 都会改变结果）。h4_sell_2 mining test_wr=58% → backtest WR=30.8%、PnL=-76
# 是这个差异的实证。所以引入"realized 门"：spec 必须在 BacktestEngine 跑过
# 一遍，实测 wr/pnl/n 都达标才能晋级 demo binding。
BACKTEST_VERIFICATION_GATES: Dict[str, float] = {
    "min_realized_wr": 0.45,  # 实测 WR >= 45%（含成本，比 mining 宽松一档）
    "min_realized_pnl": 0.0,  # 实测净盈利
    "min_realized_n": 20,  # 实测样本至少 20 笔（少于此判据不足）
}


def _parse_condition(raw: Mapping[str, Any]) -> MinedRuleCondition:
    """mining JSON 中 condition dict → MinedRuleCondition。

    mining 输出键名是 `operator`，spec 是 `op`，做名字归一化。
    """
    return MinedRuleCondition(
        indicator=str(raw["indicator"]),
        field=str(raw["field"]),
        op=str(raw.get("op") or raw["operator"]),
        threshold=float(raw["threshold"]),
    )


def _flatten_conditions(structured: Mapping[str, Any]) -> List[MinedRuleCondition]:
    """why + when + where 展平成单一条件序列（顺序：why → when → where）。"""
    conditions: List[MinedRuleCondition] = []
    for role in ("why", "when", "where"):
        for c in structured.get(role, []) or []:
            conditions.append(_parse_condition(c))
    return conditions


def _spec_from_rule(
    rule: Mapping[str, Any],
    *,
    tf: str,
    run_id: str,
    index: int,
) -> MinedRuleSpec | None:
    """单条 mining rule → MinedRuleSpec。

    返回 None 表示 rule 缺关键字段（无 conditions / 无 barrier_stats）—— 跳过而不是
    raise，让上游可以批量处理含部分残缺的 mining 输出。
    """
    structured = rule.get("structured") or {}
    conditions = _flatten_conditions(structured)
    if not conditions:
        return None

    barrier_stats = rule.get("barrier_stats_train") or []
    if not barrier_stats:
        return None
    top_barrier = barrier_stats[0]
    try:
        barrier = MinedRuleBarrier(
            sl_atr=float(top_barrier["sl_atr"]),
            tp_atr=float(top_barrier["tp_atr"]),
            time_bars=int(top_barrier["time_bars"]),
        )
    except (KeyError, TypeError, ValueError):
        return None

    direction = str(rule.get("direction", "")).lower()
    if direction not in ("buy", "sell"):
        return None

    train = rule.get("train") or {}
    test = rule.get("test") or {}

    name = f"structured_mined_{tf.lower()}_{direction}_{index}"

    return MinedRuleSpec(
        name=name,
        direction=direction,  # type: ignore[arg-type]
        timeframe=tf,
        conditions=tuple(conditions),
        barrier=barrier,
        mining_run_id=run_id,
        train_wr=float(train.get("hit_rate", 0.0) or 0.0),
        test_wr=float(test.get("hit_rate", 0.0) or 0.0),
        train_n=int(train.get("n_samples", 0) or 0),
        test_n=int(test.get("n_samples", 0) or 0),
        barrier_wr=float(top_barrier.get("hit_rate", 0.0) or 0.0),
        train_mean_return=float(train.get("mean_return", 0.0) or 0.0),
    )


def extract_specs_from_mining_json(
    payload: Mapping[str, Any],
) -> List[MinedRuleSpec]:
    """从 mining_runner JSON payload 提取所有 MinedRuleSpec。

    遍历 results[*].mined_rules，按 (tf, direction, index) 给每条 rule
    生成唯一 name；缺字段的 rule 静默跳过（不 raise）。
    """
    specs: List[MinedRuleSpec] = []
    for tf_result in payload.get("results", []) or []:
        tf = str(tf_result.get("tf", ""))
        run_id = str(tf_result.get("run_id", ""))
        rules = tf_result.get("mined_rules", []) or []
        for index, rule in enumerate(rules):
            spec = _spec_from_rule(rule, tf=tf, run_id=run_id, index=index)
            if spec is not None:
                specs.append(spec)
    return specs


def filter_promotable(specs: Sequence[MinedRuleSpec]) -> List[MinedRuleSpec]:
    """按 PROMOTION_GATES 筛选可晋级的 spec。"""
    promoted: List[MinedRuleSpec] = []
    for spec in specs:
        if spec.train_wr < PROMOTION_GATES["min_train_wr"]:
            continue
        if spec.test_wr < PROMOTION_GATES["min_test_wr"]:
            continue
        if spec.test_n < PROMOTION_GATES["min_test_n"]:
            continue
        if spec.train_wr > 0:
            drop = (spec.train_wr - spec.test_wr) / spec.train_wr
            if drop > PROMOTION_GATES["max_train_test_drop"]:
                continue
        if spec.barrier_wr < PROMOTION_GATES["min_barrier_wr"]:
            continue
        if spec.train_mean_return < PROMOTION_GATES["min_mean_return"]:
            continue
        promoted.append(spec)
    return promoted


def load_specs_from_path(path: Path | str) -> List[MinedRuleSpec]:
    """从 mining JSON 文件加载并提取 specs（不做筛选）。

    便于 catalog / scripts 直接调用。筛选请显式调 filter_promotable。
    """
    with Path(path).open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return extract_specs_from_mining_json(payload)


def filter_by_backtest(
    specs: Sequence[MinedRuleSpec],
    backtest_stats: Mapping[str, Mapping[str, Any]],
    *,
    min_realized_wr: float = BACKTEST_VERIFICATION_GATES["min_realized_wr"],
    min_realized_pnl: float = BACKTEST_VERIFICATION_GATES["min_realized_pnl"],
    min_realized_n: int = int(BACKTEST_VERIFICATION_GATES["min_realized_n"]),
) -> List[MinedRuleSpec]:
    """Stage 2 — 用 BacktestEngine 实测统计过滤 specs。

    `backtest_stats` 取 backtest_runner 输出的 `strategy_stats` 字段，
    格式 `{spec.name: {"n": int, "w": int, "pnl": float}}`。

    为何需要此 stage（不能仅用 mining-time 的 PROMOTION_GATES）：
    - mining 的 train/test hit-rate 是基于 barrier 模式纯统计推算
    - BacktestEngine 跑的链路含 filter chain / position management /
      time_exit / signal_exit / breakeven 等真实交易语义
    - 二者背离常见且显著（h4_sell_2 mining test_wr=58% → 实测 WR=30.8%）
    - 实测 0 trades 的 spec（条件极少触发）也必须在此 stage 被剔除

    Args:
        specs: PROMOTION_GATES 通过的 mining-promoted specs
        backtest_stats: spec.name → {n, w, pnl}（缺则视作 0 trades）
        min_realized_wr: 实测 win-rate floor
        min_realized_pnl: 实测净盈利 floor
        min_realized_n: 实测样本数 floor（< 此值视作证据不足）

    Returns:
        通过两阶段门禁的 specs（demo binding candidate 集合）。
    """
    out: List[MinedRuleSpec] = []
    for spec in specs:
        stats = backtest_stats.get(spec.name)
        if not stats:
            continue
        n = int(stats.get("n", 0) or 0)
        if n < min_realized_n:
            continue
        w = int(stats.get("w", 0) or 0)
        wr = w / n if n > 0 else 0.0
        if wr < min_realized_wr:
            continue
        pnl = float(stats.get("pnl", 0.0) or 0.0)
        if pnl < min_realized_pnl:
            continue
        out.append(spec)
    return out
