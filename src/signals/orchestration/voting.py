"""跨策略多数表决引擎（Strategy Voting Engine）。

## 设计动机

当前架构中，每个策略独立发出信号，1 个策略说"买"和 9 个策略一致说"买"
在 SignalRuntime 状态机看来没有任何区别——都是 confidence=0.7 的 buy。

StrategyVotingEngine 解决这个问题：
  - 聚合同一 snapshot（symbol / timeframe / scope / bar_time）下所有策略的决策
  - 对 confidence（已经过 Regime 亲和度修正）加权求票
  - 产生一个 strategy="consensus" 的综合 SignalDecision
  - consensus 信号代表"多策略共识"，置信度更高、噪声更低

## 算法

```
buy_score  = Σ confidence  (action="buy")  / Σ all confidence
sell_score = Σ confidence  (action="sell") / Σ all confidence

if buy_score  >= consensus_threshold  →  consensus_action = "buy"
if sell_score >= consensus_threshold  →  consensus_action = "sell"
else                                  →  不产生共识（返回 None）

disagreement_factor = min(buy_score, sell_score) / 0.5 × disagreement_penalty
consensus_confidence = action_score × (1.0 - disagreement_factor)
```

## 表决结果解读

| buy_score | sell_score | 结论 |
|-----------|------------|------|
| 0.80      | 0.05       | 强力共识买入（多数策略方向一致）|
| 0.55      | 0.30       | 弱共识买入（存在分歧，confidence 降低）|
| 0.45      | 0.40       | 无共识（买卖分歧过大，不发出信号）|
| 0.10      | 0.10       | 无共识（大多数策略 hold，证据不足）|

## 集成方式

StrategyVotingEngine 被注入到 SignalRuntime：
  runtime.py process_next_event() 在完成所有策略的评估后，
  调用 voting_engine.vote(decisions, regime=regime, scope=scope)，
  若返回 SignalDecision，则将其通过与普通策略相同的状态机路径发出。

consensus 信号不替代各策略独立信号，而是并行发出，
可作为更高可信度的自动交易触发依据。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..models import SignalDecision
from ..evaluation.regime import RegimeType


class StrategyVotingEngine:
    """对同一快照的多策略决策进行加权表决，产生共识信号。

    所有参数在构造时配置，运行时不维护任何可变状态（线程安全）。

    参数
    ----
    consensus_threshold:
        买方或卖方得票比例达到此值才产生方向性共识。
        0.40 = 40% 的总 confidence 权重在同一方向。
    min_quorum:
        产生共识所需的最少"非 hold"策略数量。
        设为 2 意味着至少 2 个策略同向才能定论。
    disagreement_penalty:
        当买卖双方都有明显票数时，对共识置信度施加的惩罚因子（0.0–1.0）。
        完全分歧（买=卖）时：confidence ×= (1 - 0.5 × penalty)
    """

    CONSENSUS_STRATEGY_NAME = "consensus"

    def __init__(
        self,
        *,
        consensus_threshold: float = 0.40,
        min_quorum: int = 2,
        disagreement_penalty: float = 0.50,
        group_name: str = "consensus",
    ) -> None:
        self._threshold = consensus_threshold
        self._min_quorum = min_quorum
        self._disagree_penalty = disagreement_penalty
        # group_name 决定投票结果信号的 strategy 字段值。
        # 默认 "consensus"（向后兼容）；命名 voting group 传入各自的 group name。
        self._group_name = group_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def vote(
        self,
        decisions: List[SignalDecision],
        *,
        regime: RegimeType,
        scope: str,
        exclude_composite: bool = True,
    ) -> Optional[SignalDecision]:
        """对决策列表进行加权表决，返回共识 SignalDecision 或 None。

        返回 None 的情况：
          - decisions 为空
          - 非 hold 策略数量不足 min_quorum（证据不足）
          - 买卖均未达到 consensus_threshold（方向不明确）

        返回值的 strategy 字段始终为 "consensus"，
        confidence 已包含 disagreement_penalty 修正。

        参数
        ----
        exclude_composite:
            若为 True（默认），过滤掉 metadata["composite"]=True 的复合策略决策。
            复合策略在内部已聚合了多个子策略，若再参与全局投票，
            相同指标的信号会被重复计入，造成置信度虚高。
            过滤后 consensus 仅基于独立单策略的决策，语义更清晰。
        """
        if not decisions:
            return None

        # ── 过滤复合策略，防止双重计票 ────────────────────────────────
        # CompositeSignalStrategy 在其 evaluate() 返回值的 metadata 中标记
        # "composite": True（见 composite.py）。这些策略内部已聚合了 2-4 个子策略，
        # 若同时让其参与 VotingEngine，对应指标的信号权重会被计入两次：
        # 一次来自子策略独立运行，一次来自复合策略的汇总输出。
        if exclude_composite:
            decisions = [d for d in decisions if not d.metadata.get("composite")]
        if not decisions:
            return None

        # ── 统计各方向权重 ─────────────────────────────────────────────
        buy_weight: float = 0.0
        sell_weight: float = 0.0
        hold_weight: float = 0.0
        buy_strategies: List[str] = []
        sell_strategies: List[str] = []

        for d in decisions:
            if d.action == "buy":
                buy_weight += d.confidence
                buy_strategies.append(d.strategy)
            elif d.action == "sell":
                sell_weight += d.confidence
                sell_strategies.append(d.strategy)
            else:
                hold_weight += d.confidence

        total_weight = buy_weight + sell_weight + hold_weight
        if total_weight < 1e-9:
            return None

        # ── Quorum 检查：非 hold 策略数量 ──────────────────────────────
        non_hold_count = len(buy_strategies) + len(sell_strategies)
        if non_hold_count < self._min_quorum:
            return None

        # ── 计算方向比例 ──────────────────────────────────────────────
        buy_score = buy_weight / total_weight
        sell_score = sell_weight / total_weight

        # ── 分歧惩罚 ──────────────────────────────────────────────────
        # 买卖双方都有权重时，用较小方的比例衡量分歧程度。
        # 分歧因子 ∈ [0, 1]：0 = 纯单方向（无分歧），1 = 完全对等（最大分歧）
        min_side = min(buy_score, sell_score)
        # min_side 最大为 0.5（买卖各半），归一化到 [0,1]
        disagreement_factor = (min_side / 0.5) * self._disagree_penalty

        # ── 共识判断 ─────────────────────────────────────────────────
        if buy_score >= self._threshold and buy_score > sell_score:
            action = "buy"
            raw_confidence = buy_score
            participating = buy_strategies
        elif sell_score >= self._threshold and sell_score > buy_score:
            action = "sell"
            raw_confidence = sell_score
            participating = sell_strategies
        else:
            return None  # 方向不明，不产生共识

        consensus_confidence = raw_confidence * (1.0 - disagreement_factor)

        # ── 汇总使用的指标 ────────────────────────────────────────────
        all_indicators: List[str] = []
        seen: set = set()
        for d in decisions:
            for ind in d.used_indicators:
                if ind not in seen:
                    seen.add(ind)
                    all_indicators.append(ind)

        symbol = decisions[0].symbol
        timeframe = decisions[0].timeframe

        reason = (
            f"vote:{action}={buy_score:.2f}|{sell_score:.2f},"
            f"quorum={non_hold_count}/{len(decisions)},"
            f"regime={regime.value}"
        )

        return SignalDecision(
            strategy=self._group_name,
            symbol=symbol,
            timeframe=timeframe,
            action=action,
            confidence=consensus_confidence,
            reason=reason,
            used_indicators=all_indicators,
            metadata={
                "vote_buy_weight": round(buy_weight, 4),
                "vote_sell_weight": round(sell_weight, 4),
                "vote_hold_weight": round(hold_weight, 4),
                "buy_score": round(buy_score, 4),
                "sell_score": round(sell_score, 4),
                "quorum": non_hold_count,
                "total_strategies": len(decisions),
                "regime": regime.value,
                "scope": scope,
                "disagreement_factor": round(disagreement_factor, 4),
                "participating_strategies": participating,
                "buy_strategies": buy_strategies,
                "sell_strategies": sell_strategies,
            },
        )

    def describe(self) -> Dict[str, object]:
        """返回当前配置的可读描述，用于监控端点。"""
        return {
            "group_name": self._group_name,
            "consensus_threshold": self._threshold,
            "min_quorum": self._min_quorum,
            "disagreement_penalty": self._disagree_penalty,
        }
