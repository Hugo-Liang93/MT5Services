"""复合信号策略（CompositeSignalStrategy）。

## 设计思路

单一技术指标有固有的局限性：
  - RSI 在趋势市中会产生逆势信号
  - MACD 在震荡市中频繁交叉产生假信号

复合信号通过**多重指标相互确认**来提升信号质量：只有多个子策略同时指向
同一方向时，才发出信号。代价是信号频率降低，但精度（Win Rate）更高。

## 与 VotingEngine 的区别

| 维度 | VotingEngine | CompositeSignalStrategy |
|------|-------------|------------------------|
| 作用范围 | 所有策略的全局投票 | 特定子集的局部组合 |
| 输出名称 | "consensus" | 自定义名称（如 "bb_breakout_combo"） |
| 参与投票 | 不参与（避免重复计票） | 作为普通策略参与 VotingEngine |
| 子策略数量 | 全量（8-10个） | 通常 2-4 个相关指标 |
| regime_affinity | N/A（全局） | 独立配置，往往比子策略更极端 |
| 适用场景 | 跨类型信号整合 | 同类型指标多重确认 |

## 组合模式

| 模式 | 说明 | 适用场景 |
|------|------|---------|
| `all_agree` | 全部子策略同向 → 发信号 | 高精度，低频率，适合趋势双确认 |
| `majority` | 超半数同向（> N/2） | 平衡精度与频率 |
| `weighted_sum` | 按置信度加权计票 | 子策略置信度差异大时更公平 |

## 快速上手

```python
# 趋势+动量双确认（趋势市专用）
trend_momentum = CompositeSignalStrategy(
    name="trend_momentum_combo",
    sub_strategies=[SupertrendStrategy(), MacdMomentumStrategy(), SmaTrendStrategy()],
    combine_mode="majority",
    regime_affinity={
        RegimeType.TRENDING:  1.00,
        RegimeType.RANGING:   0.10,
        RegimeType.BREAKOUT:  0.60,
        RegimeType.UNCERTAIN: 0.45,
    },
)

# 突破三重确认（突破市专用）
breakout_triple = CompositeSignalStrategy(
    name="breakout_triple_confirm",
    sub_strategies=[BollingerBreakoutStrategy(), DonchianBreakoutStrategy(), KeltnerBollingerSqueezeStrategy()],
    combine_mode="all_agree",
    regime_affinity={
        RegimeType.TRENDING:  0.50,
        RegimeType.RANGING:   0.20,
        RegimeType.BREAKOUT:  1.00,
        RegimeType.UNCERTAIN: 0.55,
    },
)
```
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Dict, Iterable, List, Literal, Optional

from ..models import SignalContext, SignalDecision
from ..evaluation.regime import RegimeType

logger = logging.getLogger(__name__)

CombineMode = Literal["all_agree", "majority", "weighted_sum"]


class CompositeSignalStrategy:
    """将多个子策略的决策融合为单一可评分信号。

    本类完全符合 ``SignalStrategy`` Protocol，可直接注册到 ``SignalModule``，
    与其他策略一起参与 ``StrategyVotingEngine`` 表决，经历相同的状态机流程。

    参数
    ----
    name:
        唯一策略标识，建议以 ``_combo`` 结尾以便区分。
    sub_strategies:
        参与组合的子策略实例列表，**不应**与 SignalModule 中已注册的同名策略重复，
        否则会导致同一策略的决策被重复计票（建议复合策略使用独立实例）。
    combine_mode:
        组合模式，见上方表格。
    regime_affinity:
        该复合策略在不同行情类型下的置信度乘数（0.0–1.0）。
        通常比子策略**更极端**：双重确认在适合行情下更可靠，
        在不适合行情下更应压制（二重虚假信号 > 单重）。
    preferred_scopes:
        接收快照的 scope，通常与子策略保持一致。
    """

    def __init__(
        self,
        name: str,
        sub_strategies: Iterable,
        *,
        combine_mode: CombineMode = "majority",
        regime_affinity: Optional[Dict[RegimeType, float]] = None,
        preferred_scopes: tuple[str, ...] = ("confirmed",),
    ) -> None:
        self.name = name
        self.category = "composite"
        self._sub_strategies: List = list(sub_strategies)
        self._combine_mode: CombineMode = combine_mode
        self.preferred_scopes = preferred_scopes
        self.regime_affinity: Dict[RegimeType, float] = regime_affinity or {}
        # required_indicators = 所有子策略的并集（运行时据此筛选 indicators 快照）
        self.required_indicators: tuple[str, ...] = tuple(
            {ind for s in self._sub_strategies for ind in getattr(s, "required_indicators", ())}
        )

    # ------------------------------------------------------------------
    # SignalStrategy Protocol
    # ------------------------------------------------------------------

    def evaluate(self, context: SignalContext) -> SignalDecision:
        """运行所有子策略并融合结果。"""
        sub_decisions: List[SignalDecision] = []
        for strategy in self._sub_strategies:
            try:
                d = strategy.evaluate(context)
                sub_decisions.append(d)
            except Exception as exc:
                logger.warning(
                    "CompositeStrategy %s: sub-strategy %s failed: %s",
                    self.name, getattr(strategy, "name", "?"), exc,
                )

        action, confidence, reason = self._combine(sub_decisions)
        used: List[str] = []
        seen: set = set()
        for d in sub_decisions:
            for ind in d.used_indicators:
                if ind not in seen:
                    seen.add(ind)
                    used.append(ind)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=reason,
            used_indicators=used,
            metadata={
                "composite": True,
                "combine_mode": self._combine_mode,
                "sub_count": len(self._sub_strategies),
                "sub_decisions": [d.to_dict() for d in sub_decisions],
            },
        )

    # ------------------------------------------------------------------
    # Combine logic
    # ------------------------------------------------------------------

    def _combine(
        self, decisions: List[SignalDecision]
    ) -> tuple[str, float, str]:
        if not decisions:
            return "hold", 0.0, "no_sub_decisions"

        buys = [d for d in decisions if d.action == "buy"]
        sells = [d for d in decisions if d.action == "sell"]
        n = len(decisions)

        if self._combine_mode == "all_agree":
            return self._all_agree(buys, sells, n)
        if self._combine_mode == "majority":
            return self._majority(buys, sells, n)
        # weighted_sum
        return self._weighted_sum(buys, sells, decisions)

    @staticmethod
    def _all_agree(
        buys: List[SignalDecision], sells: List[SignalDecision], n: int
    ) -> tuple[str, float, str]:
        """全部子策略方向一致才出信号，一个 hold/反向即为无共识。"""
        if len(buys) == n:
            conf = sum(d.confidence for d in buys) / n
            return "buy", conf, f"all_{n}_agree_buy"
        if len(sells) == n:
            conf = sum(d.confidence for d in sells) / n
            return "sell", conf, f"all_{n}_agree_sell"
        return (
            "hold", 0.0,
            f"no_consensus(buy={len(buys)},sell={len(sells)},other={n-len(buys)-len(sells)})"
        )

    @staticmethod
    def _majority(
        buys: List[SignalDecision], sells: List[SignalDecision], n: int
    ) -> tuple[str, float, str]:
        """超过半数子策略同向才出信号，置信度按多数方比例加权。"""
        threshold = n / 2.0
        if len(buys) > threshold and len(buys) > len(sells):
            # 置信度 = 平均值 × 赞成比例（人数比带来的额外加权）
            conf = (sum(d.confidence for d in buys) / len(buys)) * (len(buys) / n)
            return "buy", conf, f"majority_buy({len(buys)}/{n})"
        if len(sells) > threshold and len(sells) > len(buys):
            conf = (sum(d.confidence for d in sells) / len(sells)) * (len(sells) / n)
            return "sell", conf, f"majority_sell({len(sells)}/{n})"
        return "hold", 0.0, f"no_majority(buy={len(buys)},sell={len(sells)},n={n})"

    @staticmethod
    def _weighted_sum(
        buys: List[SignalDecision],
        sells: List[SignalDecision],
        all_decisions: List[SignalDecision],
    ) -> tuple[str, float, str]:
        """按置信度权重投票，类似 VotingEngine 但仅作用于子集。"""
        total = sum(d.confidence for d in all_decisions)
        if total < 1e-9:
            return "hold", 0.0, "zero_confidence"

        buy_score = sum(d.confidence for d in buys) / total
        sell_score = sum(d.confidence for d in sells) / total

        # 分歧惩罚：与 VotingEngine 统一使用平方衰减
        # 轻度分歧（0.55 vs 0.45）惩罚较轻，重度分歧（0.50 vs 0.50）严厉惩罚
        min_side = min(buy_score, sell_score)
        raw_disagreement = min_side / 0.5  # 归一化到 [0,1]
        penalty = 1.0 - (raw_disagreement ** 2) * 0.5

        if buy_score > sell_score and buy_score >= 0.40:
            return "buy", buy_score * penalty, f"weighted_buy({buy_score:.2f})"
        if sell_score > buy_score and sell_score >= 0.40:
            return "sell", sell_score * penalty, f"weighted_sell({sell_score:.2f})"
        return "hold", 0.0, f"weighted_no_consensus(b={buy_score:.2f},s={sell_score:.2f})"

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def describe(self) -> Dict:
        return {
            "name": self.name,
            "combine_mode": self._combine_mode,
            "sub_strategies": [getattr(s, "name", str(s)) for s in self._sub_strategies],
            "required_indicators": list(self.required_indicators),
            "preferred_scopes": list(self.preferred_scopes),
            "regime_affinity": {k.value: v for k, v in self.regime_affinity.items()},
        }


# ---------------------------------------------------------------------------
# 预置复合策略实例（可直接导入并注册到 SignalModule）
# ---------------------------------------------------------------------------

def build_trend_triple_confirm() -> "CompositeSignalStrategy":
    """趋势三重确认：Supertrend + EmaRibbon + MacdMomentum 全部同向才出信号。

    三个趋势指标同时指向同一方向，虚假信号极少，适合作为自动下单的高置信度专用通道。
    代价是信号频率很低（一天可能只有 1-3 次），但胜率更高。
    """
    from .trend import EmaRibbonStrategy, MacdMomentumStrategy, SupertrendStrategy

    return CompositeSignalStrategy(
        name="trend_triple_confirm",
        sub_strategies=[
            SupertrendStrategy(adx_threshold=25.0),
            EmaRibbonStrategy(),
            MacdMomentumStrategy(),
        ],
        combine_mode="all_agree",
        regime_affinity={
            RegimeType.TRENDING:  1.00,  # 三线同向是趋势行情最强烈的入场信号
            RegimeType.RANGING:   0.10,  # 震荡市三指标几乎不可能同向
            RegimeType.BREAKOUT:  0.50,  # 突破初期 EMA50 还未偏转，成功率约一半
            RegimeType.UNCERTAIN: 0.40,
        },
        preferred_scopes=("confirmed",),
    )


def build_breakout_double_confirm() -> "CompositeSignalStrategy":
    """突破双重确认：BollingerBreakout + DonchianBreakout 同向才出信号。

    布林带触边 + 唐奇安通道突破同时满足，大幅减少单一布林带的虚假突破信号。
    两个通道突破策略的假信号通常不会同时发生，因此双确认精度显著提升。
    """
    from .breakout import BollingerBreakoutStrategy, DonchianBreakoutStrategy

    return CompositeSignalStrategy(
        name="breakout_double_confirm",
        sub_strategies=[
            BollingerBreakoutStrategy(),
            DonchianBreakoutStrategy(adx_min=25.0),
        ],
        combine_mode="all_agree",
        regime_affinity={
            RegimeType.TRENDING:  0.40,  # 趋势中触带/通道突破属正常，不等于反转
            RegimeType.RANGING:   0.30,  # 震荡市中两个假突破仍会同时发生
            RegimeType.BREAKOUT:  1.00,  # 双通道同时突破是波动率扩张最强确认
            RegimeType.UNCERTAIN: 0.50,
        },
        preferred_scopes=("intrabar", "confirmed"),
    )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def describe(self) -> Dict:
        return {
            "name": self.name,
            "combine_mode": self._combine_mode,
            "sub_strategies": [getattr(s, "name", str(s)) for s in self._sub_strategies],
            "required_indicators": list(self.required_indicators),
            "preferred_scopes": list(self.preferred_scopes),
            "regime_affinity": {k.value: v for k, v in self.regime_affinity.items()},
        }
