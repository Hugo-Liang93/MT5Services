"""统一指标访问代理 — 访问即注册，自动推导计算集。

所有指标消费者（策略、filter、regime、sizing 等）通过 IndicatorAccessor
读取指标值。Accessor 在每次读取时自动向全局 IndicatorRegistry 注册
(consumer, indicator_name)，运行期结束后可从 registry 中提取实际被引用
的指标集合，用于下一轮按需计算。

用法:
    accessor = IndicatorAccessor(snapshot, consumer="trend_continuation")
    atr = accessor.get("atr14", "atr")          # 返回 float | None
    adx, d3 = accessor.get_many("adx14", ("adx", "adx_d3"))

    # 注册表查询
    registry = get_registry()
    registry.required_set()  # → frozenset[str] 所有被引用的指标名
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class IndicatorRegistry:
    """全局注册表 — 收集运行期所有指标访问记录。

    线程安全：多个策略/filter 可能在同一线程或不同线程中并发访问。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # consumer_name → {indicator_name, ...}
        self._accessed: dict[str, set[str]] = {}
        # 是否已完成首轮收敛（首轮全量计算，之后按需）
        self._converged = False

    def record(self, consumer: str, indicator: str) -> None:
        """记录一次指标访问。"""
        with self._lock:
            self._accessed.setdefault(consumer, set()).add(indicator)

    def required_set(self) -> frozenset[str]:
        """返回所有消费者实际访问过的指标名并集。"""
        with self._lock:
            if not self._accessed:
                return frozenset()
            return frozenset().union(*self._accessed.values())

    def consumer_requirements(self) -> dict[str, frozenset[str]]:
        """返回每个消费者各自的指标依赖集（调试/诊断用）。"""
        with self._lock:
            return {k: frozenset(v) for k, v in self._accessed.items()}

    @property
    def converged(self) -> bool:
        return self._converged

    def mark_converged(self) -> None:
        """标记首轮完成，后续可使用推导集。"""
        self._converged = True

    def declare_infrastructure(self, consumer: str, indicators: frozenset[str]) -> None:
        """基础设施组件声明其固定依赖（regime/filter/sizing 等）。

        与 record() 不同，这些依赖在启动时一次性声明，不需要运行时追踪。
        """
        with self._lock:
            existing = self._accessed.setdefault(consumer, set())
            existing.update(indicators)

    def reset(self) -> None:
        """重置注册表（测试用）。"""
        with self._lock:
            self._accessed.clear()
            self._converged = False


# ── 全局单例 ──────────────────────────────────────────────────────

_registry = IndicatorRegistry()


def get_registry() -> IndicatorRegistry:
    """获取全局 IndicatorRegistry 实例。"""
    return _registry


class IndicatorAccessor:
    """指标访问代理 — 包装一个 indicators snapshot dict，追踪访问。

    Parameters:
        snapshot: 原始指标字典 {indicator_name: {field: value}}
        consumer: 消费者标识（策略名 / filter 名 / 模块名）
        registry: 可选，默认使用全局注册表
    """

    __slots__ = ("_snapshot", "_consumer", "_registry")

    def __init__(
        self,
        snapshot: dict[str, dict[str, Any]],
        consumer: str,
        registry: Optional[IndicatorRegistry] = None,
    ) -> None:
        self._snapshot = snapshot
        self._consumer = consumer
        self._registry = registry or _registry

    @property
    def consumer(self) -> str:
        return self._consumer

    def with_consumer(self, consumer: str) -> "IndicatorAccessor":
        """以不同消费者身份创建新 accessor，共享同一 snapshot。"""
        return IndicatorAccessor(self._snapshot, consumer, self._registry)

    def get(
        self,
        indicator: str,
        field: str,
        default: Any = None,
    ) -> Any:
        """获取单个指标字段值。访问即注册。"""
        self._registry.record(self._consumer, indicator)
        payload = self._snapshot.get(indicator)
        if payload is None or not isinstance(payload, dict):
            return default
        val = payload.get(field)
        return val if val is not None else default

    def get_payload(
        self,
        indicator: str,
    ) -> dict[str, Any]:
        """获取整个指标 payload dict。访问即注册。"""
        self._registry.record(self._consumer, indicator)
        payload = self._snapshot.get(indicator)
        if payload is None or not isinstance(payload, dict):
            return {}
        return payload

    def get_many(
        self,
        indicator: str,
        fields: Sequence[str],
    ) -> Tuple[Any, ...]:
        """一次获取多个字段。返回与 fields 等长的 tuple。"""
        self._registry.record(self._consumer, indicator)
        payload = self._snapshot.get(indicator)
        if payload is None or not isinstance(payload, dict):
            return tuple(None for _ in fields)
        return tuple(payload.get(f) for f in fields)

    @property
    def raw(self) -> dict[str, dict[str, Any]]:
        """返回底层原始 dict（向后兼容，不追踪访问）。"""
        return self._snapshot
