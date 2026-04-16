"""src/research/features/hub.py

FeatureHub — 所有特征计算的单一入口。

负责：
- 根据 ResearchConfig.feature_providers 启用/禁用 Provider
- 聚合各 Provider 的数据需求
- 按序调用所有启用的 Provider，将结果注入 DataMatrix.indicator_series
- 校验特征数组长度（fail-fast）
- 提供运行时状态摘要
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from src.research.core.config import ResearchConfig
from src.research.features.protocol import (
    FeatureComputeResult,
    FeatureProvider,
    FeatureRole,
    ProviderDataRequirement,
)

logger = logging.getLogger(__name__)


class FeatureHub:
    """所有特征计算的单一调度入口。

    使用方式：
        hub = FeatureHub(config)
        result = hub.compute_all(matrix, extra_data=extra)
    """

    def __init__(self, config: ResearchConfig) -> None:
        """根据 config 注册已启用的 Provider。"""
        self._providers: Dict[str, FeatureProvider] = {}
        self._computed_keys: Dict[str, List[Tuple[str, str]]] = {}
        self._register_enabled_providers(config)

    # ------------------------------------------------------------------
    # Provider 注册（lazy import 避免 import-time 副作用）
    # ------------------------------------------------------------------

    def _register_enabled_providers(self, config: ResearchConfig) -> None:
        from src.research.features.temporal import TemporalFeatureProvider
        from src.research.features.microstructure import MicrostructureFeatureProvider
        from src.research.features.cross_tf import CrossTFFeatureProvider
        from src.research.features.regime_transition import RegimeTransitionFeatureProvider
        from src.research.features.session_event import SessionEventProvider
        from src.research.features.intrabar import IntrabarProvider

        fp = config.feature_providers

        # 各 Provider 接受各自的子配置（Optional，无参时使用默认值）
        _PROVIDER_FACTORIES: Dict[str, Any] = {
            "temporal": lambda: TemporalFeatureProvider(fp.temporal),
            "microstructure": lambda: MicrostructureFeatureProvider(fp.microstructure),
            "cross_tf": lambda: CrossTFFeatureProvider(fp.cross_tf),
            "regime_transition": lambda: RegimeTransitionFeatureProvider(fp.regime_transition),
            "session_event": lambda: SessionEventProvider(),
            "intrabar": lambda: IntrabarProvider(),
        }

        for name, factory in _PROVIDER_FACTORIES.items():
            if fp.is_enabled(name):
                self._providers[name] = factory()
                logger.info("FeatureHub: 已注册 Provider '%s'", name)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def required_extra_data(self) -> List[ProviderDataRequirement]:
        """聚合所有已启用 Provider 的额外数据需求。"""
        reqs: List[ProviderDataRequirement] = []
        for provider in self._providers.values():
            req = provider.required_extra_data()
            if req is not None:
                reqs.append(req)
        return reqs

    def compute_all(
        self,
        matrix: Any,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> FeatureComputeResult:
        """依次调用所有已启用 Provider，将特征注入 matrix.indicator_series。

        对每个 Provider：
        1. 调用 provider.compute(matrix, extra_data)
        2. 校验每个特征数组长度 == matrix.n_bars（不符合立即 raise ValueError）
        3. 注入 matrix.indicator_series
        4. 记录耗时与特征数量

        Args:
            matrix:     DataMatrix 实例
            extra_data: 由调度层准备的跨 TF 数据（可为 None）

        Returns:
            FeatureComputeResult 摘要对象
        """
        result = FeatureComputeResult()
        n_bars: int = matrix.n_bars

        for provider_name, provider in self._providers.items():
            t0 = time.perf_counter()
            features = provider.compute(matrix, extra_data)

            injected_keys: List[Tuple[str, str]] = []
            for key, values in features.items():
                if len(values) != n_bars:
                    raise ValueError(
                        f"Provider '{provider_name}' 返回特征 {key} 长度为 {len(values)}，"
                        f"期望 {n_bars}（matrix.n_bars）"
                    )
                matrix.indicator_series[key] = values
                injected_keys.append(key)

            elapsed = time.perf_counter() - t0
            feature_count = len(injected_keys)
            result.add(provider_name, feature_count, elapsed)
            self._computed_keys[provider_name] = injected_keys

            logger.info(
                "FeatureHub: Provider '%s' 完成，特征数=%d，耗时=%.3fs",
                provider_name,
                feature_count,
                elapsed,
            )

        return result

    def feature_names_by_provider(self) -> Dict[str, List[Tuple[str, str]]]:
        """返回 {provider_name: [(indicator_name, field_name), ...]}。

        仅在 compute_all() 调用后有值；调用前返回空字典。
        用于按 Provider 分组进行 FDR 检验。
        """
        return dict(self._computed_keys)

    def role_mapping_all(self) -> Dict[str, FeatureRole]:
        """聚合所有 Provider 的角色映射。

        Returns:
            {feature_field_name: FeatureRole}
        """
        merged: Dict[str, FeatureRole] = {}
        for provider in self._providers.values():
            merged.update(provider.role_mapping())
        return merged

    def describe(self) -> Dict[str, Any]:
        """返回各 Provider 的状态摘要。

        Returns:
            {
                "total_providers": int,
                "<provider_name>": {"name": str, "feature_count": int},
                ...
            }
        """
        summary: Dict[str, Any] = {"total_providers": len(self._providers)}
        for name, provider in self._providers.items():
            summary[name] = {
                "name": provider.name,
                "feature_count": provider.feature_count,
            }
        return summary
