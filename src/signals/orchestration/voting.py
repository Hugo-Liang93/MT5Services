"""跨策略多数表决引擎（Strategy Voting Engine）。

## 设计动机

当前架构中，每个策略独立发出信号，1 个策略说"买"和 9 个策略一致说"买"
在 SignalRuntime 状态机看来没有任何区别——都是 confidence=0.7 的 buy。

StrategyVotingEngine 解决这个问题：
  - 聚合同一 snapshot（symbol / timeframe / scope / bar_time）下所有策略的决策
  - 产生一个综合 SignalDecision
  - consensus 信号代表"多策略共识"

## 算法（绝对置信度锚定制）

核心原则：vote confidence 的语义必须与单策略 confidence 一致，
都表示"对这个方向判断的确信程度"，而不是"投票比例"。

```
# 1. 方向判定（仍用比例制）
buy_score  = Σ buy_confidence  / Σ all_confidence
sell_score = Σ sell_confidence / Σ all_confidence
→ 达到 consensus_threshold 则确定方向

# 2. 基础置信度（锚定在策略原始值）
avg_confidence = Σ winning_side_confidence / winning_count
→ 胜出方向策略的平均置信度，语义与单策略一致

# 3. 共识加成（多数同意给小幅奖励）
agreement_ratio = winning_count / non_hold_count   ∈ [0.5, 1.0]
consensus_bonus = normalize(agreement_ratio) × max_consensus_bonus
→ 全票同意最多 +15%，过半同意 +0%

# 4. 分歧惩罚（保留，平方衰减）
disagreement_factor = (min_side_ratio / 0.5)² × disagreement_penalty

# 5. 最终
confidence = avg_confidence × (1 + consensus_bonus) × (1 - disagreement_factor)
→ 与单策略在同一量级（0.3-0.7），可用同一个 min_confidence 阈值
```

## 旧算法 vs 新算法对比

| 场景 | 旧（比例制） | 新（锚定制） |
|------|-------------|-------------|
| 3 策略全 buy, avg conf 0.55 | 0.85+ | 0.63 |
| 5 buy(0.6) + 2 sell(0.3) | 0.72 | 0.62 |
| 2 buy(0.8) + 8 hold(0.1) | 0.67 | 0.84 |
| 10 buy(0.3) + 1 sell(0.7) | 0.59 | 0.33 |

新算法让 vote 和单策略走同一把尺子，TradeExecutor 的
per-TF min_confidence 无需为 vote 做特殊适配。
"""
from __future__ import annotations

import math
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
    max_consensus_bonus:
        当所有非 hold 策略全票同意时的最大置信度加成（0.0–0.3）。
        默认 0.15 = 全票同意时基础置信度 +15%。
    disagreement_penalty:
        当买卖双方都有明显票数时的惩罚因子（0.0–1.0）。
    """

    CONSENSUS_STRATEGY_NAME = "consensus"

    def __init__(
        self,
        *,
        consensus_threshold: float = 0.40,
        min_quorum: int = 2,
        min_quorum_ratio: float = 0.0,
        disagreement_penalty: float = 0.50,
        max_consensus_bonus: float = 0.15,
        group_name: str = "consensus",
    ) -> None:
        self._threshold = consensus_threshold
        self._min_quorum = min_quorum
        self._min_quorum_ratio = min_quorum_ratio
        self._disagreement_penalty = disagreement_penalty
        self._max_consensus_bonus = max_consensus_bonus
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
        """对决策列表进行加权表决，返回共识 SignalDecision 或 None。"""
        if not decisions:
            return None

        # ── 过滤复合策略，防止双重计票 ────────────────────────────────
        if exclude_composite:
            decisions = [d for d in decisions if not d.metadata.get("composite")]
        if not decisions:
            return None

        # ── 统计各方向 ───────────────────────────────────────────────
        buy_weight: float = 0.0
        sell_weight: float = 0.0
        hold_weight: float = 0.0
        buy_strategies: List[str] = []
        sell_strategies: List[str] = []
        buy_confidences: List[float] = []
        sell_confidences: List[float] = []

        for d in decisions:
            if d.direction == "buy":
                buy_weight += d.confidence
                buy_strategies.append(d.strategy)
                buy_confidences.append(d.confidence)
            elif d.direction == "sell":
                sell_weight += d.confidence
                sell_strategies.append(d.strategy)
                sell_confidences.append(d.confidence)
            else:
                hold_weight += d.confidence

        total_weight = buy_weight + sell_weight + hold_weight
        if total_weight < 1e-9:
            return None

        # ── Quorum 检查 ──────────────────────────────────────────────
        non_hold_count = len(buy_strategies) + len(sell_strategies)
        effective_quorum = self._min_quorum
        if self._min_quorum_ratio > 0:
            ratio_quorum = math.ceil(len(decisions) * self._min_quorum_ratio)
            effective_quorum = max(effective_quorum, ratio_quorum)
        if non_hold_count < effective_quorum:
            return None

        # ── 方向比例（仅用于判定方向，不作为 confidence） ──────────────
        buy_score = buy_weight / total_weight
        sell_score = sell_weight / total_weight

        # ── 共识方向判定 ─────────────────────────────────────────────
        if buy_score >= self._threshold and buy_score > sell_score:
            action = "buy"
            participating = buy_strategies
            winning_confidences = buy_confidences
            winning_count = len(buy_strategies)
        elif sell_score >= self._threshold and sell_score > buy_score:
            action = "sell"
            participating = sell_strategies
            winning_confidences = sell_confidences
            winning_count = len(sell_strategies)
        else:
            return None  # 方向不明，不产生共识

        # ── 基础置信度：胜出方向策略的平均 confidence ────────────────
        # 语义锚定：与单策略 confidence 在同一量级
        avg_confidence = sum(winning_confidences) / winning_count

        # ── 共识加成：多数策略同向时给予奖励 ─────────────────────────
        # agreement_ratio ∈ [0, 1]：胜出方数量占非 hold 总数的比例
        # 过半时才有加成，全票同意时达到 max_consensus_bonus
        if non_hold_count > 0:
            agreement_ratio = winning_count / non_hold_count
            # 归一化：0.5 → 0（刚过半），1.0 → 1（全票）
            normalized = max(0.0, (agreement_ratio - 0.5) / 0.5)
            consensus_bonus = normalized * self._max_consensus_bonus
        else:
            consensus_bonus = 0.0

        # ── 分歧惩罚（平方衰减，保留原设计）─────────────────────────
        min_side = min(buy_score, sell_score)
        raw_disagreement = min_side / 0.5
        disagreement_factor = (raw_disagreement ** 2) * self._disagreement_penalty

        # ── 最终 confidence ──────────────────────────────────────────
        consensus_confidence = min(
            1.0,
            avg_confidence * (1.0 + consensus_bonus) * (1.0 - disagreement_factor),
        )

        # ── 汇总指标 ────────────────────────────────────────────────
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
            f"avg_conf={avg_confidence:.3f},"
            f"bonus={consensus_bonus:.3f},"
            f"disagree={disagreement_factor:.3f},"
            f"quorum={non_hold_count}/{len(decisions)},"
            f"regime={regime.value}"
        )

        return SignalDecision(
            strategy=self._group_name,
            symbol=symbol,
            timeframe=timeframe,
            direction=action,
            confidence=consensus_confidence,
            reason=reason,
            used_indicators=all_indicators,
            metadata={
                "vote_buy_weight": round(buy_weight, 4),
                "vote_sell_weight": round(sell_weight, 4),
                "vote_hold_weight": round(hold_weight, 4),
                "buy_score": round(buy_score, 4),
                "sell_score": round(sell_score, 4),
                "avg_confidence": round(avg_confidence, 4),
                "consensus_bonus": round(consensus_bonus, 4),
                "disagreement_factor": round(disagreement_factor, 4),
                "quorum": non_hold_count,
                "total_strategies": len(decisions),
                "regime": regime.value,
                "scope": scope,
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
            "min_quorum_ratio": self._min_quorum_ratio,
            "disagreement_penalty": self._disagreement_penalty,
            "max_consensus_bonus": self._max_consensus_bonus,
        }
