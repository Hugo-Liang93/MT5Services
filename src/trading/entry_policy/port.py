"""EntryPolicy Protocol — 入场决策可插拔端口。

ADR-013 边界（强制约束）：
- 输入：intent / market / params 三个 frozen 对象，policy 只读
- 输出：EntrySpecGroup（值对象，不持有任何运行时引用）
- 禁止访问：SignalRuntime / SignalModule / PendingEntryManager / 策略实例 /
  任何 _ 前缀字段
- Policy 是纯函数式，不持有线程/锁/状态——同一 policy 实例可被并发调用

新 policy 的实现要点：
1. 定义 name 类属性（registry key）
2. 实现 derive(intent, market, params) -> EntrySpecGroup
3. 实现 describe() 返回 dict 用于诊断/审计/API
4. 注册到 EntryPolicyRegistry（factories/entry_policies.py）
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from src.trading.entry_policy.intent import EntryIntent, MarketSnapshot
from src.trading.entry_policy.specs import EntrySpecGroup

PolicyParams = Mapping[str, Any]
"""Policy 解析后的参数字典。由 EntryPolicyRegistry.resolve_params 装配。

Registry 已合并 policy_params + policy_tf_params + 转型，policy 内部直接读取
键值即可，不再做二次类型转换。键不存在时由 policy 自己提供默认值（强建议
所有键都配置在 ini，避免硬编码 default 漂移）。
"""


@runtime_checkable
class EntryPolicy(Protocol):
    """入场策略端口。"""

    name: str

    def derive(
        self,
        intent: EntryIntent,
        market: MarketSnapshot,
        params: PolicyParams,
    ) -> EntrySpecGroup:
        """根据信号意图与市场快照产出入场规格组（含单 member 退化场景）。"""
        ...

    def describe(self) -> dict[str, Any]:
        """诊断/审计端口：返回 policy 元信息（name / 适用范围 / 参数 schema）。"""
        ...


__all__ = [
    "EntryPolicy",
    "PolicyParams",
]
