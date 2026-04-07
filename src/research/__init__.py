"""信号挖掘 / 研究模块。

纯数据驱动的信号发现，位于回测的上游：
  数据挖掘(发现) → 候选规则 → 回测(验证) → 生产策略

职责边界：只看 指标值 → 未来收益 的统计关系。
不评估现有策略，不涉及 SignalModule。

核心能力：
  - Predictive Power: 指标预测力分析（IC / 命中率 / 显著性）
  - Threshold Sweep: 阈值扫描优化（最优买卖阈值 + CV 验证）
"""

from src.research.runner import MiningRunner

__all__ = ["MiningRunner"]
