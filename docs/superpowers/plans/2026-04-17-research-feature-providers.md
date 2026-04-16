# Research Feature Providers 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将挖掘模块的特征感知能力从 31 个静态快照特征扩展到 ~95 个多维度特征（时序/微结构/跨TF/Regime转换），模块化 Provider 架构。

**Architecture:** 6 个 FeatureProvider 通过 FeatureHub 统一编排。4 个新 Provider 提供新特征维度（temporal/microstructure/cross_tf/regime_transition），2 个迁移 Provider 承接现有 engineer.py 的特征（session_event/intrabar）。FeatureHub 替代 FeatureEngineer 成为唯一入口。

**Tech Stack:** Python 3.9+ / NumPy (向量化计算) / dataclasses / configparser

**Spec:** `docs/superpowers/specs/2026-04-17-research-feature-providers-design.md`

---

## 设计文档遗漏说明

Spec 中描述删除 `engineer.py` 时仅提到 3 个特征的归属，但实际有 **21 个内置特征**分 6 组：

| 现有 group | 特征数 | 迁入 Provider | 理由 |
|-----------|--------|-------------|------|
| `derived` (momentum_consensus) | 1 | 不迁移 | 已晋升为正式指标 |
| `derived` (regime_entropy, bars_in_regime) | 2 | `RegimeTransitionProvider` | regime 状态度量 |
| `derived_flow` (close_in_range, body_ratio 等) | 6 | `MicrostructureProvider` | bar 形态特征 |
| `derived_structure` (close_to_close_3, consecutive_same_color) | 2 | `MicrostructureProvider` | 多 bar 结构 |
| `derived_time` (session_phase, london/ny_session, day_progress) | 4 | `SessionEventProvider`（新增） | 时段编码 |
| `derived_event` (bars_to/since_event, in_news_window) | 3 | `SessionEventProvider`（新增） | 经济事件距离 |
| `derived_intrabar` (child_bar_consensus 等) | 5 | `IntrabarProvider`（新增） | 子 TF 特征 |

新增 `SessionEventProvider` 和 `IntrabarProvider` 两个 Provider，加上 spec 中的 4 个 = **6 个 Provider 总计**。

---

## 文件变更总览

### 新建文件

| 文件 | 职责 |
|------|------|
| `src/research/features/protocol.py` | FeatureProvider 协议 + FeatureRole + ProviderDataRequirement + FeatureComputeResult |
| `src/research/features/hub.py` | FeatureHub — 唯一编排入口 |
| `src/research/features/temporal/__init__.py` | 包导出 |
| `src/research/features/temporal/provider.py` | TemporalFeatureProvider（~34 个时序特征） |
| `src/research/features/microstructure/__init__.py` | 包导出 |
| `src/research/features/microstructure/provider.py` | MicrostructureFeatureProvider（8 迁移 + ~13 新特征） |
| `src/research/features/cross_tf/__init__.py` | 包导出 |
| `src/research/features/cross_tf/provider.py` | CrossTFFeatureProvider（~8 个跨 TF 特征） |
| `src/research/features/regime_transition/__init__.py` | 包导出 |
| `src/research/features/regime_transition/provider.py` | RegimeTransitionFeatureProvider（2 迁移 + ~9 新特征） |
| `src/research/features/session_event/__init__.py` | 包导出 |
| `src/research/features/session_event/provider.py` | SessionEventProvider（7 迁移特征：时段+事件） |
| `src/research/features/intrabar/__init__.py` | 包导出 |
| `src/research/features/intrabar/provider.py` | IntrabarProvider（5 迁移特征） |
| `tests/research/features/test_protocol.py` | 协议和类型测试 |
| `tests/research/features/test_hub.py` | Hub 编排测试 |
| `tests/research/features/test_temporal_provider.py` | 时序特征测试 |
| `tests/research/features/test_microstructure_provider.py` | 微结构特征测试 |
| `tests/research/features/test_cross_tf_provider.py` | 跨 TF 特征测试 |
| `tests/research/features/test_regime_transition_provider.py` | Regime 转换测试 |
| `tests/research/features/test_session_event_provider.py` | 时段事件测试 |
| `tests/research/features/test_intrabar_provider.py` | Intrabar 测试 |
| `tests/research/features/test_migration_parity.py` | 迁移后的数值一致性测试 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `src/research/core/config.py` | 新增 `FeatureProviderConfig` 和各 Provider 子配置 dataclass；`ResearchConfig` 替换 `feature_engineering` 字段；`load_research_config()` 解析新 ini 段 |
| `config/research.ini` | 新增 `[feature_providers]` + 各子段配置 |
| `src/research/orchestration/runner.py` | `FeatureEngineer` → `FeatureHub`；新增 extra_data 准备逻辑；传递 `provider_groups` 给分析器 |
| `src/research/analyzers/predictive_power.py` | `analyze_predictive_power()` 接受 `provider_groups` 参数，实现分组 BH-FDR |
| `src/research/analyzers/threshold.py` | 同上（如 threshold 也用 BH-FDR） |
| `src/research/analyzers/rule_mining.py` | `mine_rules()` 接受 `provider_groups` 参数，跨 Provider 规则标记 |
| `src/research/core/contracts.py` | `MiningResult` 新增 `feature_compute_summary` / `findings_by_provider` / `cross_provider_rules` 字段 |
| `src/ops/cli/mining_runner.py` | 新增 `--providers` CLI 参数；输出格式增强 |

### 删除文件

| 文件 | 理由 |
|------|------|
| `src/research/features/engineer.py` | 职责完整迁移到 Hub + 6 Providers |

---

## Task 1: 协议与类型定义（protocol.py）

**Files:**
- Create: `src/research/features/protocol.py`
- Test: `tests/research/features/test_protocol.py`

- [ ] **Step 1: 写测试 — 验证协议类型和枚举可正确实例化**

```python
# tests/research/features/test_protocol.py
"""FeatureProvider 协议与辅助类型测试。"""
import pytest
import numpy as np

from src.research.features.protocol import (
    FeatureRole,
    ProviderDataRequirement,
    FeatureComputeResult,
)


class TestFeatureRole:
    def test_enum_values(self):
        assert FeatureRole.WHY.value == "why"
        assert FeatureRole.WHEN.value == "when"
        assert FeatureRole.WHERE.value == "where"
        assert FeatureRole.VOLUME.value == "volume"


class TestProviderDataRequirement:
    def test_creation(self):
        req = ProviderDataRequirement(
            parent_tf_mapping={"M30": "H4", "H1": "H4"},
            parent_indicators=["supertrend_direction", "rsi14", "adx14"],
        )
        assert req.parent_tf_mapping["M30"] == "H4"
        assert len(req.parent_indicators) == 3


class TestFeatureComputeResult:
    def test_add_and_summary(self):
        result = FeatureComputeResult()
        result.add("temporal", 34, 0.182)
        result.add("microstructure", 13, 0.087)
        assert result.total_features == 47
        assert len(result.provider_summaries) == 2
        assert result.provider_summaries["temporal"]["feature_count"] == 34

    def test_to_dict(self):
        result = FeatureComputeResult()
        result.add("temporal", 10, 0.1)
        d = result.to_dict()
        assert "providers" in d
        assert d["total_features"] == 10
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/research/features/test_protocol.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: 实现 protocol.py**

```python
# src/research/features/protocol.py
"""Feature Provider 协议定义。

所有 Provider 实现此协议，FeatureHub 通过协议统一编排。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Tuple

import numpy as np

from ..core.data_matrix import DataMatrix


class FeatureRole(Enum):
    """特征在策略骨架中的角色映射。"""
    WHY = "why"
    WHEN = "when"
    WHERE = "where"
    VOLUME = "volume"


@dataclass(frozen=True)
class ProviderDataRequirement:
    """Provider 声明的额外数据需求（如跨 TF 需要父 TF bars）。"""
    parent_tf_mapping: Dict[str, str]   # {挖掘TF: 父TF}
    parent_indicators: List[str]        # 需要父 TF 的哪些指标


class FeatureComputeResult:
    """各 Provider 计算摘要。"""

    def __init__(self) -> None:
        self.provider_summaries: Dict[str, Dict[str, Any]] = {}

    @property
    def total_features(self) -> int:
        return sum(
            s["feature_count"] for s in self.provider_summaries.values()
        )

    def add(self, provider_name: str, feature_count: int, elapsed_sec: float) -> None:
        self.provider_summaries[provider_name] = {
            "feature_count": feature_count,
            "elapsed_sec": round(elapsed_sec, 4),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "providers": dict(self.provider_summaries),
            "total_features": self.total_features,
        }


class FeatureProvider(Protocol):
    """特征模块提供者协议。

    每个 Provider 负责一类特征的计算。
    FeatureHub 统一管理注册、配置、批量计算。
    """

    @property
    def name(self) -> str:
        """模块名，用于配置开关和日志，如 'temporal'。"""
        ...

    @property
    def feature_count(self) -> int:
        """本模块提供的特征数量。"""
        ...

    def required_columns(self) -> List[Tuple[str, str]]:
        """声明需要 DataMatrix.indicator_series 中的哪些 key。"""
        ...

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        """声明额外数据需求。None 表示只需标准 DataMatrix。"""
        ...

    def role_mapping(self) -> Dict[str, FeatureRole]:
        """声明每个特征的策略角色映射。

        key 格式: "{provider_name}_{feature_name}"
        供 StrategyCandidateSpec 生成器使用。
        """
        ...

    def compute(
        self,
        matrix: DataMatrix,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        """批量计算全部特征。

        Returns:
            {(group, feature_name): values} — 与 DataMatrix.indicator_series 兼容。
            values 长度必须等于 matrix.n_bars。
        """
        ...
```

注意：`compute()` 返回 `Dict[Tuple[str, str], List[Optional[float]]]`，直接与 `DataMatrix.indicator_series` 的 key 格式兼容。每个 Provider 用自己的 `name` 作为 group（tuple 第一元素）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/research/features/test_protocol.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/research/features/protocol.py tests/research/features/test_protocol.py
git commit -m "feat(research): add FeatureProvider protocol and types"
```

---

## Task 2: FeatureHub 编排层（hub.py）

**Files:**
- Create: `src/research/features/hub.py`
- Test: `tests/research/features/test_hub.py`

**依赖：** Task 1（protocol.py）

- [ ] **Step 1: 写测试 — 用 mock Provider 验证 Hub 编排逻辑**

```python
# tests/research/features/test_hub.py
"""FeatureHub 编排测试。"""
import pytest
from unittest.mock import MagicMock
from typing import Any, Dict, List, Optional, Tuple

from src.research.features.protocol import (
    FeatureComputeResult,
    FeatureProvider,
    FeatureRole,
    ProviderDataRequirement,
)
from src.research.features.hub import FeatureHub


def _make_mock_provider(
    name: str,
    features: Dict[Tuple[str, str], List[Optional[float]]],
) -> FeatureProvider:
    """创建 mock Provider。"""
    provider = MagicMock(spec=FeatureProvider)
    provider.name = name
    provider.feature_count = len(features)
    provider.required_columns.return_value = []
    provider.required_extra_data.return_value = None
    provider.role_mapping.return_value = {
        f"{name}_{k[1]}": FeatureRole.WHY for k in features
    }
    provider.compute.return_value = features
    return provider


class TestFeatureHub:
    def test_register_and_compute(self):
        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {}

        features_a = {("prov_a", "feat1"): [1.0, 2.0, 3.0]}
        features_b = {("prov_b", "feat2"): [4.0, 5.0, 6.0]}
        hub._providers["prov_a"] = _make_mock_provider("prov_a", features_a)
        hub._providers["prov_b"] = _make_mock_provider("prov_b", features_b)

        # 构造一个最小 DataMatrix mock
        matrix = MagicMock()
        matrix.n_bars = 3
        matrix.indicator_series = {}

        result = hub.compute_all(matrix)

        assert result.total_features == 2
        assert ("prov_a", "feat1") in matrix.indicator_series
        assert ("prov_b", "feat2") in matrix.indicator_series
        assert matrix.indicator_series[("prov_a", "feat1")] == [1.0, 2.0, 3.0]

    def test_feature_names_by_provider(self):
        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {}

        features_a = {
            ("prov_a", "f1"): [1.0],
            ("prov_a", "f2"): [2.0],
        }
        hub._providers["prov_a"] = _make_mock_provider("prov_a", features_a)

        matrix = MagicMock()
        matrix.n_bars = 1
        matrix.indicator_series = {}
        hub.compute_all(matrix)

        groups = hub.feature_names_by_provider()
        assert "prov_a" in groups
        assert ("prov_a", "f1") in groups["prov_a"]
        assert ("prov_a", "f2") in groups["prov_a"]

    def test_required_extra_data_aggregation(self):
        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {}

        p1 = _make_mock_provider("p1", {})
        p1.required_extra_data.return_value = None

        p2 = _make_mock_provider("p2", {})
        req = ProviderDataRequirement(
            parent_tf_mapping={"M30": "H4"},
            parent_indicators=["rsi14"],
        )
        p2.required_extra_data.return_value = req

        hub._providers["p1"] = p1
        hub._providers["p2"] = p2

        reqs = hub.required_extra_data()
        assert len(reqs) == 1
        assert reqs[0].parent_tf_mapping["M30"] == "H4"

    def test_role_mapping_all(self):
        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {}

        features_a = {("a", "f1"): [1.0]}
        features_b = {("b", "f2"): [2.0]}
        hub._providers["a"] = _make_mock_provider("a", features_a)
        hub._providers["b"] = _make_mock_provider("b", features_b)

        mapping = hub.role_mapping_all()
        assert "a_f1" in mapping
        assert "b_f2" in mapping

    def test_describe(self):
        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {}
        features = {("t", "f"): [1.0]}
        hub._providers["t"] = _make_mock_provider("t", features)

        desc = hub.describe()
        assert "t" in desc
        assert desc["t"]["feature_count"] == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/research/features/test_hub.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: 实现 hub.py**

```python
# src/research/features/hub.py
"""FeatureHub — 研究特征计算的唯一入口。

管理所有 FeatureProvider 的注册、配置、批量计算。
MiningRunner 只与 Hub 交互，不感知具体 Provider。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from ..core.data_matrix import DataMatrix
from .protocol import (
    FeatureComputeResult,
    FeatureProvider,
    FeatureRole,
    ProviderDataRequirement,
)

logger = logging.getLogger(__name__)


class FeatureHub:
    """研究特征计算的唯一入口。"""

    def __init__(self, config: Any) -> None:
        """根据配置实例化并注册启用的 Provider。

        Args:
            config: ResearchConfig，包含 feature_providers 配置。
        """
        self._providers: Dict[str, FeatureProvider] = {}
        self._computed_keys: Dict[str, List[Tuple[str, str]]] = {}
        self._register_enabled_providers(config)

    def _register_enabled_providers(self, config: Any) -> None:
        """根据配置决定启用哪些 Provider。"""
        from .temporal import TemporalFeatureProvider
        from .microstructure import MicrostructureFeatureProvider
        from .cross_tf import CrossTFFeatureProvider
        from .regime_transition import RegimeTransitionFeatureProvider
        from .session_event import SessionEventProvider
        from .intrabar import IntrabarProvider

        provider_classes: Dict[str, type] = {
            "temporal": TemporalFeatureProvider,
            "microstructure": MicrostructureFeatureProvider,
            "cross_tf": CrossTFFeatureProvider,
            "regime_transition": RegimeTransitionFeatureProvider,
            "session_event": SessionEventProvider,
            "intrabar": IntrabarProvider,
        }
        fp_config = config.feature_providers
        for name, cls in provider_classes.items():
            if fp_config.is_enabled(name):
                self._providers[name] = cls(config)
                logger.debug("FeatureHub: registered provider '%s'", name)

    def required_extra_data(self) -> List[ProviderDataRequirement]:
        """聚合所有 Provider 的额外数据需求，供 Runner 准备。"""
        reqs: List[ProviderDataRequirement] = []
        for provider in self._providers.values():
            req = provider.required_extra_data()
            if req is not None:
                reqs.append(req)
        return reqs

    def compute_all(
        self,
        matrix: DataMatrix,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> FeatureComputeResult:
        """批量计算全部启用模块的特征，注入 matrix.indicator_series。

        注意：直接修改 matrix.indicator_series（dict 引用），
        因为 DataMatrix.indicator_series 在 build_data_matrix 后已是可变 dict。
        """
        result = FeatureComputeResult()
        self._computed_keys.clear()

        for name, provider in self._providers.items():
            t0 = time.perf_counter()
            features = provider.compute(matrix, extra_data)
            elapsed = time.perf_counter() - t0

            keys: List[Tuple[str, str]] = []
            for feat_key, values in features.items():
                if len(values) != matrix.n_bars:
                    raise ValueError(
                        f"Provider '{name}' feature {feat_key} returned "
                        f"{len(values)} values, expected {matrix.n_bars}"
                    )
                matrix.indicator_series[feat_key] = values
                keys.append(feat_key)

            self._computed_keys[name] = keys
            result.add(name, len(features), elapsed)
            logger.info(
                "FeatureHub: '%s' computed %d features in %.3fs",
                name, len(features), elapsed,
            )

        return result

    def feature_names_by_provider(self) -> Dict[str, List[Tuple[str, str]]]:
        """返回 {provider_name: [indicator_series_keys]}，供分组 FDR。"""
        return dict(self._computed_keys)

    def role_mapping_all(self) -> Dict[str, FeatureRole]:
        """聚合全部 Provider 的角色映射，供候选生成。"""
        combined: Dict[str, FeatureRole] = {}
        for provider in self._providers.values():
            combined.update(provider.role_mapping())
        return combined

    def describe(self) -> Dict[str, Any]:
        """各模块状态摘要。"""
        return {
            name: {
                "feature_count": provider.feature_count,
                "requires_extra_data": provider.required_extra_data() is not None,
            }
            for name, provider in self._providers.items()
        }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/research/features/test_hub.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/research/features/hub.py tests/research/features/test_hub.py
git commit -m "feat(research): add FeatureHub orchestration layer"
```

---

## Task 3: 配置扩展

**Files:**
- Modify: `src/research/core/config.py`
- Modify: `config/research.ini`
- Test: `tests/research/test_config.py`（已有，扩展）

- [ ] **Step 1: 写测试 — 验证新配置数据类和 INI 解析**

```python
# tests/research/features/test_config.py
"""Feature Provider 配置测试。"""
import pytest

from src.research.core.config import (
    FeatureProviderConfig,
    TemporalProviderConfig,
    MicrostructureProviderConfig,
    CrossTFProviderConfig,
    RegimeTransitionProviderConfig,
    load_research_config,
)


class TestFeatureProviderConfig:
    def test_default_enabled(self):
        cfg = FeatureProviderConfig()
        assert cfg.is_enabled("temporal") is True
        assert cfg.is_enabled("microstructure") is True
        assert cfg.is_enabled("cross_tf") is False
        assert cfg.is_enabled("regime_transition") is True
        assert cfg.is_enabled("session_event") is True
        assert cfg.is_enabled("intrabar") is True

    def test_temporal_defaults(self):
        cfg = TemporalProviderConfig()
        assert cfg.core_indicators == ["rsi14", "adx14"]
        assert cfg.windows == [3, 5, 10]

    def test_cross_tf_parent_mapping(self):
        cfg = CrossTFProviderConfig()
        assert cfg.parent_tf_map["M30"] == "H4"
        assert cfg.parent_tf_map["H1"] == "H4"

    def test_load_from_ini_includes_providers(self):
        config = load_research_config()
        assert hasattr(config, "feature_providers")
        assert config.feature_providers.is_enabled("temporal") is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/research/features/test_config.py -v`
Expected: FAIL — new classes not found

- [ ] **Step 3: 在 config.py 中新增 Provider 配置数据类**

在 `src/research/core/config.py` 中新增以下 dataclass（在 `FeatureEngineeringConfig` 之后）：

```python
@dataclass(frozen=True)
class TemporalProviderConfig:
    core_indicators: List[str] = field(
        default_factory=lambda: ["rsi14", "adx14"]
    )
    aux_indicators: List[str] = field(
        default_factory=lambda: ["macd_histogram", "cci20", "roc12", "stoch_k"]
    )
    windows: List[int] = field(default_factory=lambda: [3, 5, 10])
    cross_levels_rsi: List[float] = field(default_factory=lambda: [30.0, 50.0, 70.0])
    cross_levels_adx: List[float] = field(default_factory=lambda: [20.0, 25.0])


@dataclass(frozen=True)
class MicrostructureProviderConfig:
    lookback: int = 5


@dataclass(frozen=True)
class CrossTFProviderConfig:
    parent_tf_map: Dict[str, str] = field(
        default_factory=lambda: {"M15": "H1", "M30": "H4", "H1": "H4", "H4": "D1"}
    )
    parent_indicators: List[str] = field(
        default_factory=lambda: [
            "supertrend_direction", "rsi14", "adx14", "ema50", "bb_position",
        ]
    )


@dataclass(frozen=True)
class RegimeTransitionProviderConfig:
    history_window: int = 50
    prob_delta_window: int = 5


@dataclass(frozen=True)
class FeatureProviderConfig:
    """所有 Provider 的启用开关和子配置。"""
    temporal_enabled: bool = True
    microstructure_enabled: bool = True
    cross_tf_enabled: bool = False
    regime_transition_enabled: bool = True
    session_event_enabled: bool = True
    intrabar_enabled: bool = True

    temporal: TemporalProviderConfig = field(default_factory=TemporalProviderConfig)
    microstructure: MicrostructureProviderConfig = field(
        default_factory=MicrostructureProviderConfig
    )
    cross_tf: CrossTFProviderConfig = field(default_factory=CrossTFProviderConfig)
    regime_transition: RegimeTransitionProviderConfig = field(
        default_factory=RegimeTransitionProviderConfig
    )

    # FDR 分组策略
    fdr_grouping: str = "by_provider"  # "by_provider" | "global" | "none"

    def is_enabled(self, provider_name: str) -> bool:
        attr = f"{provider_name}_enabled"
        return getattr(self, attr, True)
```

在 `ResearchConfig` 中用 `feature_providers` 替换 `feature_engineering`：

```python
@dataclass(frozen=True)
class ResearchConfig:
    # ... 现有字段不变 ...
    feature_providers: FeatureProviderConfig = field(
        default_factory=FeatureProviderConfig
    )
    # 保留 feature_engineering 供过渡期读取，但标记废弃
    feature_engineering: FeatureEngineeringConfig = field(
        default_factory=FeatureEngineeringConfig
    )
```

在 `load_research_config()` 中新增 `[feature_providers]` 段解析逻辑。

- [ ] **Step 4: 更新 config/research.ini 新增 Provider 配置段**

在 `config/research.ini` 末尾追加：

```ini
[feature_providers]
temporal = true
microstructure = true
cross_tf = false
regime_transition = true
session_event = true
intrabar = true
fdr_grouping = by_provider

[feature_providers.temporal]
core_indicators = rsi14,adx14
aux_indicators = macd_histogram,cci20,roc12,stoch_k
windows = 3,5,10
cross_levels_rsi = 30,50,70
cross_levels_adx = 20,25

[feature_providers.microstructure]
lookback = 5

[feature_providers.cross_tf]
parent_tf_map = M15:H1,M30:H4,H1:H4,H4:D1
parent_indicators = supertrend_direction,rsi14,adx14,ema50,bb_position

[feature_providers.regime_transition]
history_window = 50
prob_delta_window = 5
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/research/features/test_config.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/research/core/config.py config/research.ini tests/research/features/test_config.py
git commit -m "feat(research): add feature provider configuration"
```

---

## Task 4: RegimeTransitionProvider（迁移 + 新特征）

**Files:**
- Create: `src/research/features/regime_transition/__init__.py`
- Create: `src/research/features/regime_transition/provider.py`
- Test: `tests/research/features/test_regime_transition_provider.py`

**迁移：** `regime_entropy` + `bars_in_regime` 从 engineer.py
**新增：** `bars_since_change`, `prev_regime`, `transitions_{w}`, `trending/ranging/breakout_prob_delta_{w}`, `duration_vs_avg`, `entropy_delta_{w}`, `dominant_strength`

- [ ] **Step 1: 写测试**

```python
# tests/research/features/test_regime_transition_provider.py
"""RegimeTransitionProvider 测试。"""
import math
import pytest
from unittest.mock import MagicMock

from src.research.features.regime_transition.provider import (
    RegimeTransitionFeatureProvider,
)
from src.research.features.protocol import FeatureRole


def _make_matrix(
    n_bars: int,
    regimes: list,
    soft_regimes: list,
) -> MagicMock:
    matrix = MagicMock()
    matrix.n_bars = n_bars
    matrix.regimes = regimes
    matrix.soft_regimes = soft_regimes
    matrix.indicator_series = {}
    return matrix


class TestRegimeTransitionProvider:
    def _make_provider(self):
        config = MagicMock()
        config.feature_providers.regime_transition.history_window = 50
        config.feature_providers.regime_transition.prob_delta_window = 5
        return RegimeTransitionFeatureProvider(config)

    def test_name_and_feature_count(self):
        p = self._make_provider()
        assert p.name == "regime_transition"
        assert p.feature_count > 0

    def test_no_extra_data_required(self):
        p = self._make_provider()
        assert p.required_extra_data() is None

    def test_regime_entropy_migration(self):
        """验证迁移后的 regime_entropy 与原始实现数值一致。"""
        p = self._make_provider()

        # 均匀分布 → 最大熵
        soft = [{"trending": 0.25, "ranging": 0.25, "breakout": 0.25, "uncertain": 0.25}]
        matrix = _make_matrix(1, [MagicMock(value="trending")], soft)
        features = p.compute(matrix)
        entropy_key = ("regime_transition", "regime_entropy")
        assert entropy_key in features
        expected = -4 * (0.25 * math.log(0.25))
        assert abs(features[entropy_key][0] - expected) < 1e-6

    def test_bars_in_regime_migration(self):
        """验证迁移后的 bars_in_regime 与原始实现数值一致。"""
        p = self._make_provider()
        r1 = MagicMock(value="trending")
        r2 = MagicMock(value="ranging")
        regimes = [r1, r1, r1, r2, r2]
        soft = [None] * 5
        matrix = _make_matrix(5, regimes, soft)
        features = p.compute(matrix)
        key = ("regime_transition", "bars_in_regime")
        assert features[key] == [1.0, 2.0, 3.0, 1.0, 2.0]

    def test_bars_since_regime_change(self):
        p = self._make_provider()
        r1 = MagicMock(value="trending")
        r2 = MagicMock(value="ranging")
        regimes = [r1, r1, r2, r2, r2]
        soft = [None] * 5
        matrix = _make_matrix(5, regimes, soft)
        features = p.compute(matrix)
        key = ("regime_transition", "bars_since_change")
        # bar 0: 无前一个→0, bar 1: 同→1, bar 2: 变→0, bar 3: 同→1, bar 4: 同→2
        assert features[key] == [0.0, 1.0, 0.0, 1.0, 2.0]

    def test_role_mapping(self):
        p = self._make_provider()
        mapping = p.role_mapping()
        assert mapping.get("regime_transition_bars_since_change") == FeatureRole.WHEN
        assert mapping.get("regime_transition_regime_entropy") == FeatureRole.WHEN
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/research/features/test_regime_transition_provider.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 Provider**

```python
# src/research/features/regime_transition/__init__.py
from .provider import RegimeTransitionFeatureProvider

__all__ = ["RegimeTransitionFeatureProvider"]
```

```python
# src/research/features/regime_transition/provider.py
"""RegimeTransitionFeatureProvider — Regime 转换特征。

包含从 engineer.py 迁移的 regime_entropy + bars_in_regime，
以及新增的转换动态特征。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ...core.data_matrix import DataMatrix
from ..protocol import FeatureProvider, FeatureRole, ProviderDataRequirement

_GROUP = "regime_transition"


class RegimeTransitionFeatureProvider:
    """Regime 转换特征计算。"""

    def __init__(self, config: Any) -> None:
        rt_cfg = config.feature_providers.regime_transition
        self._history_window = rt_cfg.history_window
        self._prob_delta_window = rt_cfg.prob_delta_window

    @property
    def name(self) -> str:
        return _GROUP

    @property
    def feature_count(self) -> int:
        # 迁移 2 + 新增: bars_since_change, prev_regime, transitions_{w},
        # 3 prob deltas, duration_vs_avg, entropy_delta, dominant_strength = 9
        return 11

    def required_columns(self) -> List[Tuple[str, str]]:
        return []  # 使用 matrix.regimes 和 matrix.soft_regimes

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        return None

    def role_mapping(self) -> Dict[str, FeatureRole]:
        return {
            f"{_GROUP}_regime_entropy": FeatureRole.WHEN,
            f"{_GROUP}_bars_in_regime": FeatureRole.WHEN,
            f"{_GROUP}_bars_since_change": FeatureRole.WHEN,
            f"{_GROUP}_prev_regime": FeatureRole.WHY,
            f"{_GROUP}_transitions_{self._prob_delta_window}": FeatureRole.WHEN,
            f"{_GROUP}_trending_prob_delta_{self._prob_delta_window}": FeatureRole.WHY,
            f"{_GROUP}_ranging_prob_delta_{self._prob_delta_window}": FeatureRole.WHY,
            f"{_GROUP}_breakout_prob_delta_{self._prob_delta_window}": FeatureRole.WHY,
            f"{_GROUP}_duration_vs_avg": FeatureRole.WHEN,
            f"{_GROUP}_entropy_delta_{self._prob_delta_window}": FeatureRole.WHEN,
            f"{_GROUP}_dominant_strength": FeatureRole.WHY,
        }

    def compute(
        self,
        matrix: DataMatrix,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        n = matrix.n_bars
        w = self._prob_delta_window
        results: Dict[Tuple[str, str], List[Optional[float]]] = {}

        # ── 迁移: regime_entropy ──
        entropy_vals: List[Optional[float]] = []
        for sr in matrix.soft_regimes:
            if sr is None:
                entropy_vals.append(None)
                continue
            probs = [p for p in sr.values() if p > 0]
            if not probs:
                entropy_vals.append(None)
            else:
                entropy_vals.append(-sum(p * math.log(p) for p in probs))
        results[(_GROUP, "regime_entropy")] = entropy_vals

        # ── 迁移: bars_in_regime (连续当前 regime bar 数) ──
        bars_in = [1.0] * n
        for i in range(1, n):
            if matrix.regimes[i] == matrix.regimes[i - 1]:
                bars_in[i] = bars_in[i - 1] + 1.0
        results[(_GROUP, "bars_in_regime")] = bars_in

        # ── 新增: bars_since_change (距上次 regime 变化) ──
        since_change: List[Optional[float]] = [0.0] * n
        count = 0.0
        for i in range(1, n):
            if matrix.regimes[i] != matrix.regimes[i - 1]:
                count = 0.0
            else:
                count += 1.0
            since_change[i] = count
        results[(_GROUP, "bars_since_change")] = since_change

        # ── 新增: prev_regime (上一段 regime 编码) ──
        _REGIME_CODES = {"trending": 0.0, "ranging": 1.0, "breakout": 2.0, "uncertain": 3.0}
        prev_regime_vals: List[Optional[float]] = [None] * n
        last_different = None
        for i in range(1, n):
            if matrix.regimes[i] != matrix.regimes[i - 1]:
                last_different = matrix.regimes[i - 1]
            if last_different is not None:
                val = last_different.value if hasattr(last_different, "value") else str(last_different)
                prev_regime_vals[i] = _REGIME_CODES.get(val)
        results[(_GROUP, "prev_regime")] = prev_regime_vals

        # ── 新增: transitions_{w} (过去 w bar 内切换次数) ──
        transitions: List[Optional[float]] = [None] * n
        for i in range(w, n):
            count_t = 0.0
            for j in range(i - w + 1, i + 1):
                if matrix.regimes[j] != matrix.regimes[j - 1]:
                    count_t += 1.0
            transitions[i] = count_t
        results[(_GROUP, f"transitions_{w}")] = transitions

        # ── 新增: soft regime prob deltas ──
        for regime_name in ("trending", "ranging", "breakout"):
            delta_vals: List[Optional[float]] = [None] * n
            for i in range(w, n):
                sr_cur = matrix.soft_regimes[i]
                sr_prev = matrix.soft_regimes[i - w]
                if sr_cur is not None and sr_prev is not None:
                    cur_p = sr_cur.get(regime_name, 0.0)
                    prev_p = sr_prev.get(regime_name, 0.0)
                    delta_vals[i] = cur_p - prev_p
            results[(_GROUP, f"{regime_name}_prob_delta_{w}")] = delta_vals

        # ── 新增: duration_vs_avg ──
        # 计算各 regime 的历史平均持续 bar 数
        duration_vs: List[Optional[float]] = [None] * n
        regime_durations: Dict[str, List[float]] = {}
        seg_start = 0
        for i in range(1, n):
            if matrix.regimes[i] != matrix.regimes[i - 1]:
                rv = matrix.regimes[seg_start].value if hasattr(matrix.regimes[seg_start], "value") else str(matrix.regimes[seg_start])
                regime_durations.setdefault(rv, []).append(float(i - seg_start))
                seg_start = i
        # 最后一段
        rv = matrix.regimes[seg_start].value if hasattr(matrix.regimes[seg_start], "value") else str(matrix.regimes[seg_start])
        regime_durations.setdefault(rv, []).append(float(n - seg_start))

        regime_avg: Dict[str, float] = {
            k: sum(v) / len(v) for k, v in regime_durations.items()
        }
        for i in range(n):
            cur_rv = matrix.regimes[i].value if hasattr(matrix.regimes[i], "value") else str(matrix.regimes[i])
            avg = regime_avg.get(cur_rv, 1.0)
            if avg > 0:
                duration_vs[i] = bars_in[i] / avg
        results[(_GROUP, "duration_vs_avg")] = duration_vs

        # ── 新增: entropy_delta_{w} ──
        entropy_delta: List[Optional[float]] = [None] * n
        for i in range(w, n):
            if entropy_vals[i] is not None and entropy_vals[i - w] is not None:
                entropy_delta[i] = entropy_vals[i] - entropy_vals[i - w]
        results[(_GROUP, f"entropy_delta_{w}")] = entropy_delta

        # ── 新增: dominant_strength (max soft prob) ──
        dominant: List[Optional[float]] = []
        for sr in matrix.soft_regimes:
            if sr is None:
                dominant.append(None)
            else:
                dominant.append(max(sr.values()) if sr else None)
        results[(_GROUP, "dominant_strength")] = dominant

        return results
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/research/features/test_regime_transition_provider.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/research/features/regime_transition/ tests/research/features/test_regime_transition_provider.py
git commit -m "feat(research): add RegimeTransitionFeatureProvider"
```

---

## Task 5: MicrostructureProvider（迁移 + 新特征）

**Files:**
- Create: `src/research/features/microstructure/__init__.py`
- Create: `src/research/features/microstructure/provider.py`
- Test: `tests/research/features/test_microstructure_provider.py`

**迁移：** close_in_range, body_ratio, upper_wick_ratio, lower_wick_ratio, range_expansion, oc_imbalance, close_to_close_3, consecutive_same_color（8 个）
**新增：** consecutive_up/down, vol_price_accord, volatility_ratio, bb_width_change, avg_body_ratio, upper/lower_shadow_ratio (rolling), hh/ll_count, gap_ratio, range_position, volume_surge（~13 个）

- [ ] **Step 1: 写测试**

```python
# tests/research/features/test_microstructure_provider.py
"""MicrostructureFeatureProvider 测试。"""
import pytest
from unittest.mock import MagicMock

from src.research.features.microstructure.provider import (
    MicrostructureFeatureProvider,
)
from src.research.features.protocol import FeatureRole


def _make_matrix(n: int, **kwargs) -> MagicMock:
    """创建包含 OHLCV 数据的 mock DataMatrix。"""
    matrix = MagicMock()
    matrix.n_bars = n
    matrix.opens = kwargs.get("opens", [100.0] * n)
    matrix.highs = kwargs.get("highs", [102.0] * n)
    matrix.lows = kwargs.get("lows", [98.0] * n)
    matrix.closes = kwargs.get("closes", [101.0] * n)
    matrix.volumes = kwargs.get("volumes", [1000.0] * n)
    matrix.indicator_series = kwargs.get("indicator_series", {})
    return matrix


class TestMicrostructureProvider:
    def _make_provider(self):
        config = MagicMock()
        config.feature_providers.microstructure.lookback = 5
        return MicrostructureFeatureProvider(config)

    def test_name(self):
        p = self._make_provider()
        assert p.name == "microstructure"

    def test_close_in_range_migration(self):
        """验证 close_in_range 迁移后数值一致。"""
        p = self._make_provider()
        matrix = _make_matrix(
            3,
            opens=[100, 100, 100],
            highs=[110, 110, 110],
            lows=[90, 90, 90],
            closes=[105, 90, 110],
        )
        features = p.compute(matrix)
        key = ("microstructure", "close_in_range")
        assert key in features
        assert abs(features[key][0] - 0.75) < 1e-6  # (105-90)/(110-90)
        assert abs(features[key][1] - 0.0) < 1e-6   # (90-90)/(110-90)
        assert abs(features[key][2] - 1.0) < 1e-6   # (110-90)/(110-90)

    def test_body_ratio_migration(self):
        p = self._make_provider()
        matrix = _make_matrix(
            1,
            opens=[100],
            highs=[110],
            lows=[90],
            closes=[110],
        )
        features = p.compute(matrix)
        key = ("microstructure", "body_ratio")
        assert abs(features[key][0] - 0.5) < 1e-6  # |110-100|/20

    def test_consecutive_up(self):
        p = self._make_provider()
        matrix = _make_matrix(
            5,
            closes=[100, 101, 102, 103, 102],
        )
        features = p.compute(matrix)
        key = ("microstructure", "consecutive_up")
        # bar 0: 0, bar 1: 1, bar 2: 2, bar 3: 3, bar 4: 0 (下跌)
        assert features[key][3] == 3.0
        assert features[key][4] == 0.0

    def test_volume_surge(self):
        p = self._make_provider()
        matrix = _make_matrix(
            6,
            volumes=[100, 100, 100, 100, 100, 500],
        )
        features = p.compute(matrix)
        key = ("microstructure", "volume_surge")
        # bar 5: 500 / mean([100]*5) = 5.0
        assert features[key][5] == pytest.approx(5.0, rel=0.01)

    def test_role_mapping(self):
        p = self._make_provider()
        mapping = p.role_mapping()
        assert mapping["microstructure_consecutive_up"] == FeatureRole.WHY
        assert mapping["microstructure_volume_surge"] == FeatureRole.VOLUME
        assert mapping["microstructure_close_in_range"] == FeatureRole.WHERE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/research/features/test_microstructure_provider.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 Provider**

实现 `src/research/features/microstructure/provider.py`，包含：
- 迁移的 8 个现有特征（numpy 向量化实现，从 engineer.py 的 `_batch_*` 函数移植）
- 新增的 13 个微结构特征
- 所有计算使用 numpy 向量化（O(n)），无逐 bar 循环

关键实现模式（所有特征统一）：

```python
class MicrostructureFeatureProvider:
    _GROUP = "microstructure"

    def __init__(self, config: Any) -> None:
        self._lookback = config.feature_providers.microstructure.lookback

    def compute(self, matrix, extra_data=None):
        n = matrix.n_bars
        w = self._lookback
        results = {}

        highs = np.asarray(matrix.highs, dtype=np.float64)
        lows = np.asarray(matrix.lows, dtype=np.float64)
        opens = np.asarray(matrix.opens, dtype=np.float64)
        closes = np.asarray(matrix.closes, dtype=np.float64)
        volumes = np.asarray(matrix.volumes, dtype=np.float64)
        rng = highs - lows
        safe_rng = np.where(rng < 1e-9, 1.0, rng)

        # ── 迁移特征（向量化） ──
        results[(self._GROUP, "close_in_range")] = np.where(
            rng < 1e-9, 0.5, (closes - lows) / safe_rng
        ).tolist()

        results[(self._GROUP, "body_ratio")] = np.where(
            rng < 1e-9, 0.0, np.abs(closes - opens) / safe_rng
        ).tolist()

        # ... upper_wick_ratio, lower_wick_ratio, oc_imbalance,
        # range_expansion, close_to_close_3, consecutive_same_color
        # （从 engineer.py 的 _batch_* 函数直接移植向量化逻辑）

        # ── 新增特征 ──
        # consecutive_up / consecutive_down
        close_delta = np.diff(closes, prepend=closes[0])
        up = (close_delta > 0).astype(np.float64)
        down = (close_delta < 0).astype(np.float64)
        # 连续计数用递推
        consec_up = np.zeros(n)
        consec_down = np.zeros(n)
        for i in range(1, n):
            consec_up[i] = (consec_up[i-1] + 1) * up[i]
            consec_down[i] = (consec_down[i-1] + 1) * down[i]
        results[(self._GROUP, "consecutive_up")] = consec_up.tolist()
        results[(self._GROUP, "consecutive_down")] = consec_down.tolist()

        # volume_surge: vol[i] / mean(vol[i-w:i])
        vol_surge = [None] * n
        for i in range(w, n):
            avg = np.mean(volumes[i-w:i])
            vol_surge[i] = float(volumes[i] / avg) if avg > 0 else None
        results[(self._GROUP, "volume_surge")] = vol_surge

        # ... 其余新特征同理
        return results
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/research/features/test_microstructure_provider.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/research/features/microstructure/ tests/research/features/test_microstructure_provider.py
git commit -m "feat(research): add MicrostructureFeatureProvider (migration + new)"
```

---

## Task 6: TemporalFeatureProvider（全新特征）

**Files:**
- Create: `src/research/features/temporal/__init__.py`
- Create: `src/research/features/temporal/provider.py`
- Test: `tests/research/features/test_temporal_provider.py`

**全部新增：** delta, accel, slope, zscore, bars_since_cross 对可配置指标和窗口

- [ ] **Step 1: 写测试**

```python
# tests/research/features/test_temporal_provider.py
"""TemporalFeatureProvider 测试。"""
import pytest
import numpy as np
from unittest.mock import MagicMock

from src.research.features.temporal.provider import TemporalFeatureProvider
from src.research.features.protocol import FeatureRole


def _make_matrix(n: int, rsi_values: list, adx_values: list) -> MagicMock:
    matrix = MagicMock()
    matrix.n_bars = n
    matrix.indicator_series = {
        ("rsi14", "rsi"): rsi_values,
        ("adx14", "adx"): adx_values,
    }
    return matrix


class TestTemporalFeatureProvider:
    def _make_provider(self):
        config = MagicMock()
        cfg = config.feature_providers.temporal
        cfg.core_indicators = ["rsi14", "adx14"]
        cfg.aux_indicators = []
        cfg.windows = [3]
        cfg.cross_levels_rsi = [50.0]
        cfg.cross_levels_adx = [20.0]
        return TemporalFeatureProvider(config)

    def test_delta(self):
        """delta_3 = value[i] - value[i-3]"""
        p = self._make_provider()
        rsi = [30.0, 35.0, 40.0, 45.0, 50.0]
        adx = [10.0, 12.0, 14.0, 16.0, 18.0]
        matrix = _make_matrix(5, rsi, adx)
        features = p.compute(matrix)
        key = ("temporal", "rsi14_delta_3")
        assert key in features
        # bar 3: 45 - 30 = 15, bar 4: 50 - 35 = 15
        assert features[key][3] == pytest.approx(15.0)
        assert features[key][4] == pytest.approx(15.0)
        assert features[key][2] is None  # not enough bars

    def test_slope(self):
        """slope_3 = 线性回归斜率"""
        p = self._make_provider()
        rsi = [10.0, 20.0, 30.0, 40.0]
        adx = [10.0] * 4
        matrix = _make_matrix(4, rsi, adx)
        features = p.compute(matrix)
        key = ("temporal", "rsi14_slope_3")
        # 完美线性：斜率 = 10.0/bar
        assert features[key][3] == pytest.approx(10.0, rel=0.01)

    def test_bars_since_cross(self):
        """bars_since_cross_50: RSI 最近穿越 50 的 bar 数。"""
        p = self._make_provider()
        rsi = [40.0, 45.0, 55.0, 60.0, 65.0]  # bar 2 穿越 50
        adx = [10.0] * 5
        matrix = _make_matrix(5, rsi, adx)
        features = p.compute(matrix)
        key = ("temporal", "rsi14_bars_since_cross_50")
        assert key in features
        # bar 2: 穿越 → 0, bar 3: 1, bar 4: 2
        assert features[key][2] == 0.0
        assert features[key][3] == 1.0
        assert features[key][4] == 2.0

    def test_feature_count(self):
        """2 核心 × 1 窗口 × 4 类型 + bars_since_cross"""
        p = self._make_provider()
        assert p.feature_count > 0

    def test_role_mapping(self):
        p = self._make_provider()
        mapping = p.role_mapping()
        assert mapping["temporal_rsi14_delta_3"] == FeatureRole.WHY
        assert mapping["temporal_rsi14_bars_since_cross_50"] == FeatureRole.WHEN
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/research/features/test_temporal_provider.py -v`

- [ ] **Step 3: 实现 Provider**

实现 `src/research/features/temporal/provider.py`。核心逻辑：

```python
class TemporalFeatureProvider:
    _GROUP = "temporal"

    def __init__(self, config):
        t_cfg = config.feature_providers.temporal
        self._core_indicators = t_cfg.core_indicators
        self._aux_indicators = t_cfg.aux_indicators
        self._windows = t_cfg.windows
        # 构建 cross levels 映射：{indicator_series_key: [levels]}
        self._cross_levels = {}
        # RSI cross levels 映射到 ("rsi14", "rsi") key
        if "rsi14" in self._core_indicators:
            self._cross_levels[("rsi14", "rsi")] = t_cfg.cross_levels_rsi
        if "adx14" in self._core_indicators:
            self._cross_levels[("adx14", "adx")] = t_cfg.cross_levels_adx

    def _indicator_series_key(self, ind_name: str) -> tuple:
        """指标名 → indicator_series 中的 (indicator, field) key。"""
        _MAPPING = {
            "rsi14": ("rsi14", "rsi"),
            "adx14": ("adx14", "adx"),
            "macd_histogram": ("macd", "hist"),
            "cci20": ("cci20", "cci"),
            "roc12": ("roc12", "roc"),
            "stoch_k": ("stoch_rsi14", "stoch_rsi_k"),
        }
        return _MAPPING.get(ind_name, (ind_name, ind_name))

    def compute(self, matrix, extra_data=None):
        results = {}

        # 核心指标：全量时序特征（delta/accel/slope/zscore × 所有窗口）
        for ind_name in self._core_indicators:
            series_key = self._indicator_series_key(ind_name)
            raw = matrix.indicator_series.get(series_key)
            if raw is None:
                continue
            values = np.array([v if v is not None else np.nan for v in raw])

            for w in self._windows:
                results[(self._GROUP, f"{ind_name}_delta_{w}")] = self._delta(values, w)
                results[(self._GROUP, f"{ind_name}_accel_{w}")] = self._accel(values, w)
                results[(self._GROUP, f"{ind_name}_slope_{w}")] = self._slope(values, w)
                results[(self._GROUP, f"{ind_name}_zscore_{w}")] = self._zscore(values, w)

            # bars_since_cross
            if series_key in self._cross_levels:
                for level in self._cross_levels[series_key]:
                    results[(self._GROUP, f"{ind_name}_bars_since_cross_{int(level)}")] = (
                        self._bars_since_cross(values, level)
                    )

        # 辅助指标：仅 delta 对最大窗口
        max_w = max(self._windows)
        for ind_name in self._aux_indicators:
            series_key = self._indicator_series_key(ind_name)
            raw = matrix.indicator_series.get(series_key)
            if raw is None:
                continue
            values = np.array([v if v is not None else np.nan for v in raw])
            results[(self._GROUP, f"{ind_name}_delta_{max_w}")] = self._delta(values, max_w)

        return results

    @staticmethod
    def _delta(values: np.ndarray, w: int) -> List[Optional[float]]:
        n = len(values)
        out: List[Optional[float]] = [None] * n
        for i in range(w, n):
            if np.isfinite(values[i]) and np.isfinite(values[i - w]):
                out[i] = float(values[i] - values[i - w])
        return out

    @staticmethod
    def _accel(values: np.ndarray, w: int) -> List[Optional[float]]:
        n = len(values)
        out: List[Optional[float]] = [None] * n
        for i in range(2 * w, n):
            d_cur = values[i] - values[i - w]
            d_prev = values[i - w] - values[i - 2 * w]
            if np.isfinite(d_cur) and np.isfinite(d_prev):
                out[i] = float(d_cur - d_prev)
        return out

    @staticmethod
    def _slope(values: np.ndarray, w: int) -> List[Optional[float]]:
        n = len(values)
        out: List[Optional[float]] = [None] * n
        x = np.arange(w + 1, dtype=np.float64)
        for i in range(w, n):
            window = values[i - w: i + 1]
            if np.all(np.isfinite(window)):
                # 线性回归斜率 = cov(x,y) / var(x)
                out[i] = float(np.polyfit(x, window, 1)[0])
        return out

    @staticmethod
    def _zscore(values: np.ndarray, w: int) -> List[Optional[float]]:
        n = len(values)
        out: List[Optional[float]] = [None] * n
        for i in range(w, n):
            window = values[i - w: i + 1]
            if np.all(np.isfinite(window)):
                m = np.mean(window)
                s = np.std(window)
                if s > 1e-9:
                    out[i] = float((values[i] - m) / s)
        return out

    @staticmethod
    def _bars_since_cross(values: np.ndarray, level: float) -> List[Optional[float]]:
        n = len(values)
        out: List[Optional[float]] = [None] * n
        last_cross = -1
        for i in range(1, n):
            if np.isfinite(values[i]) and np.isfinite(values[i - 1]):
                if (values[i - 1] < level <= values[i]) or (values[i - 1] > level >= values[i]):
                    last_cross = i
            if last_cross >= 0:
                out[i] = float(i - last_cross)
        return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/research/features/test_temporal_provider.py -v`

- [ ] **Step 5: 提交**

```bash
git add src/research/features/temporal/ tests/research/features/test_temporal_provider.py
git commit -m "feat(research): add TemporalFeatureProvider"
```

---

## Task 7: SessionEventProvider + IntrabarProvider（迁移）

**Files:**
- Create: `src/research/features/session_event/{__init__,provider}.py`
- Create: `src/research/features/intrabar/{__init__,provider}.py`
- Test: `tests/research/features/test_session_event_provider.py`
- Test: `tests/research/features/test_intrabar_provider.py`

这两个 Provider 纯迁移现有特征，无新增。从 engineer.py 的 `_batch_*` 函数移植向量化实现。

- [ ] **Step 1: 写 SessionEventProvider 测试**

```python
# tests/research/features/test_session_event_provider.py
"""SessionEventProvider 测试（迁移验证）。"""
import pytest
from datetime import datetime
from unittest.mock import MagicMock

from src.research.features.session_event.provider import SessionEventProvider


class TestSessionEventProvider:
    def _make_provider(self):
        config = MagicMock()
        return SessionEventProvider(config)

    def test_session_phase(self):
        p = self._make_provider()
        matrix = MagicMock()
        matrix.n_bars = 4
        matrix.bar_times = [
            datetime(2025, 1, 1, 3, 0),   # Asia → 0
            datetime(2025, 1, 1, 8, 0),   # London → 1
            datetime(2025, 1, 1, 15, 0),  # NY → 2
            datetime(2025, 1, 1, 22, 0),  # Close → 3
        ]
        matrix.high_impact_event_times = ()
        matrix.timeframe = "H1"
        matrix.indicator_series = {}
        features = p.compute(matrix)
        key = ("session_event", "session_phase")
        assert features[key] == [0.0, 1.0, 2.0, 3.0]

    def test_in_news_window(self):
        p = self._make_provider()
        matrix = MagicMock()
        matrix.n_bars = 3
        ev_time = datetime(2025, 1, 1, 10, 0)
        matrix.bar_times = [
            datetime(2025, 1, 1, 9, 0),   # 60 min before → 0
            datetime(2025, 1, 1, 9, 45),  # 15 min before → 1
            datetime(2025, 1, 1, 12, 0),  # 2 hours after → 0
        ]
        matrix.high_impact_event_times = (ev_time,)
        matrix.timeframe = "M15"
        matrix.indicator_series = {}
        features = p.compute(matrix)
        key = ("session_event", "in_news_window")
        assert features[key][0] == 0.0
        assert features[key][1] == 1.0
        assert features[key][2] == 0.0
```

- [ ] **Step 2: 写 IntrabarProvider 测试**

```python
# tests/research/features/test_intrabar_provider.py
"""IntrabarProvider 测试（迁移验证）。"""
import pytest
from unittest.mock import MagicMock

from src.research.features.intrabar.provider import IntrabarProvider


class TestIntrabarProvider:
    def _make_provider(self):
        config = MagicMock()
        return IntrabarProvider(config)

    def test_skips_when_no_child_bars(self):
        p = self._make_provider()
        matrix = MagicMock()
        matrix.n_bars = 3
        matrix.child_bars = {}
        matrix.opens = [100.0] * 3
        matrix.closes = [101.0] * 3
        matrix.child_tf = ""
        matrix.indicator_series = {}
        features = p.compute(matrix)
        # 应返回空 dict（无 child_bars 时不计算）
        assert len(features) == 0
```

- [ ] **Step 3: 运行测试确认失败**

Run: `pytest tests/research/features/test_session_event_provider.py tests/research/features/test_intrabar_provider.py -v`

- [ ] **Step 4: 实现两个 Provider**

**SessionEventProvider** — 迁移 7 个特征：session_phase, london_session, ny_session, day_progress, bars_to_next_high_impact_event, bars_since_last_high_impact_event, in_news_window。从 engineer.py 的 `_batch_*` 函数直接移植。

**IntrabarProvider** — 迁移 5 个特征：child_bar_consensus, child_range_acceleration, intrabar_momentum_shift, child_volume_front_weight, child_bar_count_ratio。当 `matrix.child_bars` 为空时返回空 dict（不计算）。

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/research/features/test_session_event_provider.py tests/research/features/test_intrabar_provider.py -v`

- [ ] **Step 6: 提交**

```bash
git add src/research/features/session_event/ src/research/features/intrabar/ \
  tests/research/features/test_session_event_provider.py \
  tests/research/features/test_intrabar_provider.py
git commit -m "feat(research): add SessionEventProvider + IntrabarProvider (migration)"
```

---

## Task 8: CrossTFFeatureProvider（全新特征）

**Files:**
- Create: `src/research/features/cross_tf/__init__.py`
- Create: `src/research/features/cross_tf/provider.py`
- Test: `tests/research/features/test_cross_tf_provider.py`

**全部新增：** parent_trend_dir, parent_rsi, parent_adx, tf_trend_align, dist_to_parent_ema, parent_rsi_delta_5, parent_adx_delta_5, parent_bb_pos

**关键：** 需要 extra_data（父 TF bars + 指标），通过 `required_extra_data()` 声明。

- [ ] **Step 1: 写测试**

```python
# tests/research/features/test_cross_tf_provider.py
"""CrossTFFeatureProvider 测试。"""
import pytest
import numpy as np
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from src.research.features.cross_tf.provider import CrossTFFeatureProvider
from src.research.features.protocol import FeatureRole


class TestCrossTFProvider:
    def _make_provider(self):
        config = MagicMock()
        cfg = config.feature_providers.cross_tf
        cfg.parent_tf_map = {"M30": "H4"}
        cfg.parent_indicators = ["supertrend_direction", "rsi14"]
        return CrossTFFeatureProvider(config)

    def test_requires_extra_data(self):
        p = self._make_provider()
        req = p.required_extra_data()
        assert req is not None
        assert "M30" in req.parent_tf_mapping

    def test_parent_trend_dir_forward_fill(self):
        """验证父 TF 值正确 forward-fill 到子 TF。"""
        p = self._make_provider()
        # 4 个 M30 bars 属于 1 个 H4 bar
        base = datetime(2025, 1, 1, 0, 0)
        matrix = MagicMock()
        matrix.n_bars = 4
        matrix.timeframe = "M30"
        matrix.bar_times = [base + timedelta(minutes=30 * i) for i in range(4)]
        matrix.closes = [100.0, 101.0, 102.0, 103.0]
        matrix.indicator_series = {
            ("supertrend", "direction"): [1.0, 1.0, 1.0, 1.0],
        }

        extra_data = {
            "parent_tf": "H4",
            "parent_bar_times": [base],
            "parent_indicators": {
                "supertrend_direction": [1.0],
                "rsi14": [55.0],
            },
        }
        features = p.compute(matrix, extra_data)
        key = ("cross_tf", "parent_trend_dir")
        assert key in features
        # 所有 4 个 M30 bar 都映射到同一个 H4 bar
        assert all(v == 1.0 for v in features[key])

    def test_returns_empty_without_extra_data(self):
        p = self._make_provider()
        matrix = MagicMock()
        matrix.n_bars = 3
        matrix.indicator_series = {}
        features = p.compute(matrix, extra_data=None)
        assert len(features) == 0

    def test_role_mapping(self):
        p = self._make_provider()
        mapping = p.role_mapping()
        assert mapping["cross_tf_parent_trend_dir"] == FeatureRole.WHY
        assert mapping["cross_tf_dist_to_parent_ema"] == FeatureRole.WHERE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/research/features/test_cross_tf_provider.py -v`

- [ ] **Step 3: 实现 Provider**

核心逻辑：用 `np.searchsorted` + forward-fill 对齐父 TF 指标到子 TF bar。

```python
class CrossTFFeatureProvider:
    _GROUP = "cross_tf"

    def compute(self, matrix, extra_data=None):
        if extra_data is None:
            return {}

        results = {}
        n = matrix.n_bars
        parent_times = extra_data["parent_bar_times"]
        parent_inds = extra_data["parent_indicators"]

        # 对齐：每个子 TF bar 映射到最近的父 TF bar（forward-fill）
        child_ts = np.array([t.timestamp() for t in matrix.bar_times])
        parent_ts = np.array([t.timestamp() for t in parent_times])
        # searchsorted side='right' - 1 = 最近的 <= child_ts 的父 bar
        idx = np.searchsorted(parent_ts, child_ts, side="right") - 1
        idx = np.clip(idx, 0, len(parent_ts) - 1)

        # 父 TF 指标值对齐到子 TF
        for ind_name, parent_vals in parent_inds.items():
            arr = np.array(parent_vals, dtype=np.float64)
            aligned = arr[idx]
            results[(self._GROUP, f"parent_{ind_name}")] = aligned.tolist()

        # 派生: tf_trend_align, dist_to_parent_ema, delta 等
        # ...
        return results
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/research/features/test_cross_tf_provider.py -v`

- [ ] **Step 5: 提交**

```bash
git add src/research/features/cross_tf/ tests/research/features/test_cross_tf_provider.py
git commit -m "feat(research): add CrossTFFeatureProvider"
```

---

## Task 9: 迁移数值一致性验证

**Files:**
- Test: `tests/research/features/test_migration_parity.py`

验证所有从 engineer.py 迁移的特征在新 Provider 中产出完全一致的数值。

- [ ] **Step 1: 写一致性测试**

```python
# tests/research/features/test_migration_parity.py
"""迁移数值一致性测试：新 Provider vs 旧 engineer.py 的输出必须完全一致。"""
import math
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from src.research.features.engineer import build_default_engineer
from src.research.features.microstructure.provider import MicrostructureFeatureProvider
from src.research.features.regime_transition.provider import RegimeTransitionFeatureProvider
from src.research.features.session_event.provider import SessionEventProvider


def _build_test_matrix():
    """构建一个包含真实数据分布的 DataMatrix mock。"""
    n = 50
    matrix = MagicMock()
    matrix.n_bars = n
    # 价格数据（模拟 XAUUSD 波动）
    base_price = 2000.0
    import numpy as np
    np.random.seed(42)
    returns = np.random.randn(n) * 0.002
    closes = [base_price]
    for r in returns[1:]:
        closes.append(closes[-1] * (1 + r))
    matrix.closes = closes
    matrix.opens = [c - np.random.uniform(0, 2) for c in closes]
    matrix.highs = [max(o, c) + np.random.uniform(0, 3) for o, c in zip(matrix.opens, closes)]
    matrix.lows = [min(o, c) - np.random.uniform(0, 3) for o, c in zip(matrix.opens, closes)]
    matrix.volumes = [1000 + np.random.uniform(0, 500) for _ in range(n)]

    # Regime 数据
    from src.market_structure.models import RegimeType
    regimes = [RegimeType.TRENDING] * 20 + [RegimeType.RANGING] * 15 + [RegimeType.TRENDING] * 15
    matrix.regimes = regimes
    matrix.soft_regimes = [
        {"trending": 0.6, "ranging": 0.2, "breakout": 0.1, "uncertain": 0.1}
        if r == RegimeType.TRENDING else
        {"trending": 0.2, "ranging": 0.6, "breakout": 0.1, "uncertain": 0.1}
        for r in regimes
    ]

    # 时间和事件
    base_time = datetime(2025, 6, 1, 0, 0)
    matrix.bar_times = [base_time + timedelta(hours=i) for i in range(n)]
    matrix.high_impact_event_times = (
        base_time + timedelta(hours=10),
        base_time + timedelta(hours=30),
    )
    matrix.timeframe = "H1"
    matrix.child_bars = {}
    matrix.child_tf = ""

    # indicator_series
    atr_vals = [3.0 + np.random.uniform(-0.5, 0.5) for _ in range(n)]
    matrix.indicator_series = {
        ("atr14", "atr"): atr_vals,
        ("macd", "hist"): [np.random.randn() for _ in range(n)],
        ("rsi14", "rsi"): [50 + np.random.randn() * 10 for _ in range(n)],
        ("stoch_rsi14", "stoch_rsi_k"): [50 + np.random.randn() * 20 for _ in range(n)],
    }
    return matrix


_MIGRATION_MAP = {
    # (old_group, old_name): (new_provider_class, new_group, new_name)
    ("derived", "regime_entropy"): ("regime_transition", "regime_entropy"),
    ("derived", "bars_in_regime"): ("regime_transition", "bars_in_regime"),
    ("derived_flow", "close_in_range"): ("microstructure", "close_in_range"),
    ("derived_flow", "body_ratio"): ("microstructure", "body_ratio"),
    ("derived_flow", "upper_wick_ratio"): ("microstructure", "upper_wick_ratio"),
    ("derived_flow", "lower_wick_ratio"): ("microstructure", "lower_wick_ratio"),
    ("derived_flow", "range_expansion"): ("microstructure", "range_expansion"),
    ("derived_flow", "oc_imbalance"): ("microstructure", "oc_imbalance"),
    ("derived_structure", "close_to_close_3"): ("microstructure", "close_to_close_3"),
    ("derived_structure", "consecutive_same_color"): ("microstructure", "consecutive_same_color"),
    ("derived_time", "session_phase"): ("session_event", "session_phase"),
    ("derived_time", "london_session"): ("session_event", "london_session"),
    ("derived_time", "ny_session"): ("session_event", "ny_session"),
    ("derived_time", "day_progress"): ("session_event", "day_progress"),
    ("derived_event", "bars_to_next_high_impact_event"): ("session_event", "bars_to_next_high_impact_event"),
    ("derived_event", "bars_since_last_high_impact_event"): ("session_event", "bars_since_last_high_impact_event"),
    ("derived_event", "in_news_window"): ("session_event", "in_news_window"),
}


class TestMigrationParity:
    def test_all_migrated_features_match(self):
        """逐个验证迁移特征的数值完全一致。"""
        matrix = _build_test_matrix()

        # 旧实现
        engineer = build_default_engineer()
        old_matrix = engineer.enrich(matrix)

        # 新实现
        config = MagicMock()
        config.feature_providers.microstructure.lookback = 5
        config.feature_providers.regime_transition.history_window = 50
        config.feature_providers.regime_transition.prob_delta_window = 5
        micro_p = MicrostructureFeatureProvider(config)
        regime_p = RegimeTransitionFeatureProvider(config)
        session_p = SessionEventProvider(config)

        new_features = {}
        new_features.update(micro_p.compute(matrix))
        new_features.update(regime_p.compute(matrix))
        new_features.update(session_p.compute(matrix))

        for (old_group, old_name), (new_group, new_name) in _MIGRATION_MAP.items():
            old_key = (old_group, old_name)
            new_key = (new_group, new_name)
            old_vals = old_matrix.indicator_series.get(old_key)
            new_vals = new_features.get(new_key)

            assert old_vals is not None, f"Old feature {old_key} not found"
            assert new_vals is not None, f"New feature {new_key} not found"
            assert len(old_vals) == len(new_vals), f"Length mismatch for {old_name}"

            for i, (ov, nv) in enumerate(zip(old_vals, new_vals)):
                if ov is None:
                    assert nv is None, f"{old_name}[{i}]: old=None, new={nv}"
                else:
                    assert nv is not None, f"{old_name}[{i}]: old={ov}, new=None"
                    assert abs(ov - nv) < 1e-9, (
                        f"{old_name}[{i}]: old={ov}, new={nv}, diff={abs(ov-nv)}"
                    )
```

- [ ] **Step 2: 运行测试**

Run: `pytest tests/research/features/test_migration_parity.py -v`
Expected: PASS（所有迁移特征数值完全一致）

- [ ] **Step 3: 提交**

```bash
git add tests/research/features/test_migration_parity.py
git commit -m "test(research): add migration parity verification for all 17 features"
```

---

## Task 10: MiningRunner 集成

**Files:**
- Modify: `src/research/orchestration/runner.py`
- Test: `tests/research/test_runner_integration.py`（已有，扩展）

- [ ] **Step 1: 写测试 — 验证 Runner 使用 FeatureHub**

```python
# tests/research/features/test_runner_hub_integration.py
"""验证 MiningRunner 使用 FeatureHub 而非 FeatureEngineer。"""
import pytest
from unittest.mock import patch, MagicMock

from src.research.orchestration.runner import MiningRunner


class TestRunnerHubIntegration:
    @patch("src.research.orchestration.runner.FeatureHub")
    @patch("src.research.orchestration.runner.build_data_matrix")
    def test_runner_uses_feature_hub(self, mock_build_dm, mock_hub_cls):
        """确认 Runner 调用 FeatureHub.compute_all()。"""
        mock_matrix = MagicMock()
        mock_matrix.n_bars = 100
        mock_matrix.bar_times = [MagicMock()] * 100
        mock_matrix.regimes = [MagicMock(value="trending")] * 100
        mock_matrix.indicator_series = {}
        mock_matrix.available_indicator_fields.return_value = []
        mock_matrix.train_slice.return_value = range(70)
        mock_matrix.test_slice.return_value = range(70, 100)
        mock_build_dm.return_value = mock_matrix

        mock_hub = MagicMock()
        mock_hub.required_extra_data.return_value = []
        mock_hub.compute_all.return_value = MagicMock(total_features=50)
        mock_hub.feature_names_by_provider.return_value = {"temporal": []}
        mock_hub_cls.return_value = mock_hub

        runner = MiningRunner()
        # 仅跑 predictive_power 避免复杂依赖
        with patch.object(runner, "_run_predictive_power", return_value=[]):
            result = runner.run(
                "XAUUSD", "H1",
                MagicMock(), MagicMock(),
                analyses=["predictive_power"],
            )

        mock_hub.compute_all.assert_called_once()

    def test_no_import_of_feature_engineer(self):
        """确认 runner.py 不再 import FeatureEngineer。"""
        import inspect
        from src.research.orchestration import runner
        source = inspect.getsource(runner)
        assert "FeatureEngineer" not in source
        assert "build_default_engineer" not in source
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/research/features/test_runner_hub_integration.py -v`

- [ ] **Step 3: 修改 MiningRunner**

在 `src/research/orchestration/runner.py` 中：

1. 删除 `from src.research.features.engineer import build_default_engineer` 导入
2. 新增 `from src.research.features.hub import FeatureHub` 导入
3. 在 `__init__` 中创建 `self._feature_hub = FeatureHub(self._config)`
4. 在 `run()` 中替换特征工程逻辑：

```python
# 删除旧代码：
# if self._config.feature_engineering.enabled:
#     from src.research.features.engineer import build_default_engineer
#     engineer = build_default_engineer()
#     matrix = engineer.enrich(matrix, ...)

# 新代码：
# 准备额外数据（CrossTF 等）
extra_data = None
extra_reqs = self._feature_hub.required_extra_data()
if extra_reqs:
    extra_data = self._prepare_extra_data(
        symbol, timeframe, start_time, end_time, extra_reqs
    )

# 特征计算
compute_result = self._feature_hub.compute_all(matrix, extra_data)
logger.info(
    "FeatureHub: %d features computed across %d providers",
    compute_result.total_features,
    len(compute_result.provider_summaries),
)

# 传递 provider_groups 供分析器分组 FDR
provider_groups = self._feature_hub.feature_names_by_provider()
```

5. 新增 `_prepare_extra_data()` 方法：加载父 TF bars 和指标。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/research/features/test_runner_hub_integration.py -v`

- [ ] **Step 5: 提交**

```bash
git add src/research/orchestration/runner.py tests/research/features/test_runner_hub_integration.py
git commit -m "feat(research): integrate FeatureHub into MiningRunner"
```

---

## Task 11: 分析器分组 FDR 适配

**Files:**
- Modify: `src/research/analyzers/predictive_power.py`
- Modify: `src/research/analyzers/rule_mining.py`
- Test: `tests/research/analyzers/test_grouped_fdr.py`

- [ ] **Step 1: 写测试**

```python
# tests/research/analyzers/test_grouped_fdr.py
"""分组 FDR 测试。"""
import pytest
from src.research.analyzers.predictive_power import _apply_grouped_fdr


class TestGroupedFDR:
    def test_by_provider_more_lenient_than_global(self):
        """分组 FDR 应比全局 FDR 更宽松（每组独立 5%）。"""
        # 模拟 3 个 provider 各 10 个特征，其中 p=0.04 的特征
        results = []
        for prov in ("temporal", "micro", "regime"):
            for i in range(10):
                results.append({
                    "key": (prov, f"feat_{i}"),
                    "perm_pvalue": 0.04 if i == 0 else 0.5,
                })

        provider_groups = {
            "temporal": [("temporal", f"feat_{i}") for i in range(10)],
            "micro": [("micro", f"feat_{i}") for i in range(10)],
            "regime": [("regime", f"feat_{i}") for i in range(10)],
        }

        # 分组 FDR：每组 10 个检验，p=0.04 应该通过
        significant = _apply_grouped_fdr(results, provider_groups, alpha=0.05)
        assert len(significant) == 3  # 每组的 p=0.04 都通过

    def test_global_fdr_more_conservative(self):
        """全局 FDR 30 个检验，p=0.04 可能不通过。"""
        results = []
        for i in range(30):
            results.append({
                "key": ("base", f"feat_{i}"),
                "perm_pvalue": 0.04 if i < 3 else 0.5,
            })
        # 全局 FDR 应更保守
        significant = _apply_grouped_fdr(results, None, alpha=0.05)
        # BH-FDR with 30 tests, 3 with p=0.04: adjusted p = 0.04 * 30/3 = 0.4 > 0.05
        assert len(significant) <= 3
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 在 predictive_power.py 中实现分组 FDR**

在 `_apply_batch_correction()` 函数中增加 `provider_groups` 参数。当 `fdr_grouping == "by_provider"` 时，按组独立调用 `benjamini_hochberg_fdr()`。

```python
def _apply_grouped_fdr(
    results: list,
    provider_groups: Optional[Dict[str, list]],
    alpha: float = 0.05,
) -> list:
    """分组 BH-FDR 校正。

    provider_groups 为 None 时退化为全局 FDR。
    """
    from ..core.overfitting import benjamini_hochberg_fdr

    if provider_groups is None:
        # 全局 FDR
        p_values = [r["perm_pvalue"] for r in results]
        corrections = benjamini_hochberg_fdr(p_values, alpha=alpha)
        return [results[idx] for idx, _, is_sig in corrections if is_sig]

    # 分组 FDR
    significant = []
    for group_name, group_keys in provider_groups.items():
        group_key_set = set(map(tuple, group_keys))
        group_results = [r for r in results if tuple(r["key"]) in group_key_set]
        if not group_results:
            continue
        p_values = [r["perm_pvalue"] for r in group_results]
        corrections = benjamini_hochberg_fdr(p_values, alpha=alpha)
        for idx, _, is_sig in corrections:
            if is_sig:
                significant.append(group_results[idx])
    return significant
```

- [ ] **Step 4: 同步修改 rule_mining.py — 跨 Provider 规则标记**

在 `mine_rules()` 中接受 `provider_groups` 参数。规则生成后，检查规则条件涉及的 Provider 数量：

```python
def _tag_cross_provider_rules(
    rules: list,
    provider_groups: Optional[Dict[str, list]],
) -> list:
    if provider_groups is None:
        return []
    # 构建 feature_key → provider 映射
    key_to_provider = {}
    for prov, keys in provider_groups.items():
        for k in keys:
            key_to_provider[tuple(k)] = prov

    cross_rules = []
    for rule in rules:
        providers_used = set()
        for cond in rule.conditions:
            key = (cond.indicator, cond.field)
            prov = key_to_provider.get(key, "base")
            providers_used.add(prov)
        if len(providers_used) >= 2:
            cross_rules.append(rule)
    return cross_rules
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/research/analyzers/test_grouped_fdr.py -v`

- [ ] **Step 6: 提交**

```bash
git add src/research/analyzers/predictive_power.py src/research/analyzers/rule_mining.py \
  tests/research/analyzers/test_grouped_fdr.py
git commit -m "feat(research): add grouped FDR and cross-provider rule tagging"
```

---

## Task 12: MiningResult 输出增强 + CLI

**Files:**
- Modify: `src/research/core/contracts.py`
- Modify: `src/ops/cli/mining_runner.py`
- Modify: `src/research/orchestration/runner.py`

- [ ] **Step 1: 在 MiningResult 中新增字段**

在 `src/research/core/contracts.py` 的 `MiningResult` 中新增：

```python
@dataclass
class MiningResult:
    # ... 现有字段 ...

    # 新增
    feature_compute_summary: Optional[Dict[str, Any]] = None
    findings_by_provider: Dict[str, List[Finding]] = field(default_factory=dict)
    cross_provider_rules: List[Any] = field(default_factory=list)
```

- [ ] **Step 2: 在 MiningRunner._rank_findings 中按 provider 分组**

在 `runner.py` 的 `_rank_findings()` 中，利用 `provider_groups` 将 findings 分组到 `result.findings_by_provider`。

- [ ] **Step 3: CLI 新增 --providers 参数**

在 `src/ops/cli/mining_runner.py` 的 `argparse` 中：

```python
parser.add_argument(
    "--providers",
    type=str,
    default=None,
    help="启用的 Feature Provider（逗号分隔，如 temporal,microstructure）。"
         "'all' 启用全部。默认读取 research.ini 配置。",
)
```

在 `main()` 中，如果 `--providers` 指定，覆盖 config 中的启用状态。

- [ ] **Step 4: CLI 输出格式增强 — 新增 Provider 摘要段**

在 `_render_default()` 中新增 Provider 计算摘要输出：

```
Feature Providers:
  ✓ temporal         34 features   182ms
  ✓ microstructure   13 features    87ms
  ✗ cross_tf         (disabled)
  ✓ regime_transition 9 features    43ms
```

- [ ] **Step 5: 运行全量测试确认不回归**

Run: `pytest tests/research/ -v`

- [ ] **Step 6: 提交**

```bash
git add src/research/core/contracts.py src/ops/cli/mining_runner.py src/research/orchestration/runner.py
git commit -m "feat(research): enhance MiningResult output and CLI --providers flag"
```

---

## Task 13: 删除 engineer.py + 最终清理

**Files:**
- Delete: `src/research/features/engineer.py`
- Modify: 所有引用 engineer.py 的文件
- Test: 全量回归

- [ ] **Step 1: 搜索所有对 engineer.py 的引用**

```bash
grep -rn "from src.research.features.engineer" src/ tests/
grep -rn "features.engineer" src/ tests/
grep -rn "FeatureEngineer" src/ tests/
grep -rn "build_default_engineer" src/ tests/
grep -rn "get_feature_definition" src/ tests/
grep -rn "get_feature_inventory" src/ tests/
```

逐一修改或删除引用。注意：
- `candidates.py` 和 `promotion.py` 可能引用 `FeatureDefinition` 或 `PROMOTED_INDICATOR_PRECEDENTS` → 将必要常量迁移到 protocol.py 或 candidates.py 自身
- 测试文件中对 engineer 的直接引用 → 改为对新 Provider 的引用
- `test_migration_parity.py` 仍需保留对旧 engineer 的引用用于对比（可在删除后将其标记为 skip 或移除）

- [ ] **Step 2: 将 PROMOTED_INDICATOR_PRECEDENTS 迁移到 protocol.py**

```python
# 在 protocol.py 末尾添加
PROMOTED_INDICATOR_PRECEDENTS: Tuple[Dict[str, Any], ...] = (
    # ... 保持不变 ...
)
```

- [ ] **Step 3: 删除 engineer.py**

```bash
git rm src/research/features/engineer.py
```

- [ ] **Step 4: 运行全量测试**

```bash
pytest tests/ -v --tb=short
```

确认无任何引用 engineer.py 的 import 错误。

- [ ] **Step 5: 运行 mypy 类型检查**

```bash
mypy src/research/ --strict
```

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor(research): remove engineer.py, all features migrated to providers"
```

---

## Task 14: 端到端集成测试

**Files:**
- Test: `tests/research/features/test_e2e_feature_hub.py`

- [ ] **Step 1: 写端到端测试**

```python
# tests/research/features/test_e2e_feature_hub.py
"""FeatureHub 端到端集成测试 — 验证完整管线。"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
import numpy as np

from src.research.core.config import load_research_config
from src.research.features.hub import FeatureHub


@pytest.mark.integration
class TestFeatureHubE2E:
    def test_full_pipeline_default_config(self):
        """默认配置下（temporal+micro+regime+session+intrabar），
        Hub 能正确计算所有特征并注入 matrix。"""
        config = load_research_config()
        hub = FeatureHub(config)

        # 构建测试 matrix
        n = 100
        np.random.seed(42)
        matrix = MagicMock()
        matrix.n_bars = n
        matrix.opens = np.random.uniform(1990, 2010, n).tolist()
        matrix.highs = [o + np.random.uniform(1, 5) for o in matrix.opens]
        matrix.lows = [o - np.random.uniform(1, 5) for o in matrix.opens]
        matrix.closes = [o + np.random.uniform(-3, 3) for o in matrix.opens]
        matrix.volumes = np.random.uniform(500, 2000, n).tolist()
        matrix.bar_times = [
            datetime(2025, 1, 1) + timedelta(hours=i) for i in range(n)
        ]
        matrix.timeframe = "H1"
        matrix.child_bars = {}
        matrix.child_tf = ""
        matrix.high_impact_event_times = (
            datetime(2025, 1, 1, 15, 0),
        )
        from src.market_structure.models import RegimeType
        matrix.regimes = [
            RegimeType.TRENDING if i < 50 else RegimeType.RANGING
            for i in range(n)
        ]
        matrix.soft_regimes = [
            {"trending": 0.7, "ranging": 0.2, "breakout": 0.05, "uncertain": 0.05}
            if i < 50 else
            {"trending": 0.2, "ranging": 0.7, "breakout": 0.05, "uncertain": 0.05}
            for i in range(n)
        ]
        # 指标数据
        matrix.indicator_series = {
            ("rsi14", "rsi"): np.random.uniform(20, 80, n).tolist(),
            ("adx14", "adx"): np.random.uniform(10, 40, n).tolist(),
            ("atr14", "atr"): np.random.uniform(2, 5, n).tolist(),
            ("macd", "hist"): np.random.randn(n).tolist(),
            ("cci20", "cci"): np.random.uniform(-150, 150, n).tolist(),
            ("roc12", "roc"): np.random.randn(n).tolist(),
            ("stoch_rsi14", "stoch_rsi_k"): np.random.uniform(0, 100, n).tolist(),
            ("bb20", "bb_width"): np.random.uniform(5, 15, n).tolist(),
        }

        result = hub.compute_all(matrix)

        # 验证基本统计
        assert result.total_features > 50  # 至少 50 个特征
        assert "temporal" in result.provider_summaries
        assert "microstructure" in result.provider_summaries
        assert "regime_transition" in result.provider_summaries
        assert "session_event" in result.provider_summaries

        # 验证所有特征长度正确
        for key, values in matrix.indicator_series.items():
            if isinstance(key, tuple) and key[0] in (
                "temporal", "microstructure", "regime_transition", "session_event"
            ):
                assert len(values) == n, f"Feature {key} has wrong length"

        # 验证 provider_groups 包含所有特征
        groups = hub.feature_names_by_provider()
        total_from_groups = sum(len(v) for v in groups.values())
        assert total_from_groups == result.total_features

    def test_describe(self):
        config = load_research_config()
        hub = FeatureHub(config)
        desc = hub.describe()
        assert "temporal" in desc
        assert desc["temporal"]["feature_count"] > 0
```

- [ ] **Step 2: 运行端到端测试**

Run: `pytest tests/research/features/test_e2e_feature_hub.py -v`
Expected: PASS

- [ ] **Step 3: 运行全量研究模块测试**

Run: `pytest tests/research/ -v --tb=short`
Expected: PASS（无回归）

- [ ] **Step 4: 提交**

```bash
git add tests/research/features/test_e2e_feature_hub.py
git commit -m "test(research): add end-to-end FeatureHub integration test"
```

---

## Task 15: 文档更新

**Files:**
- Modify: `CLAUDE.md`（更新模块路径表）
- Modify: `docs/research-system.md`（更新特征工程章节）
- Modify: `docs/codebase-review.md`（记录架构变更）

- [ ] **Step 1: 更新 CLAUDE.md**

在 Research 模块路径表中：
- 删除 `特征工程 / 特征候选 / 特征提升` 行中对 `engineer.py` 的引用
- 新增 `FeatureHub` 和 6 个 Provider 的路径

- [ ] **Step 2: 更新 docs/research-system.md**

新增"Feature Providers"章节，描述 6 个 Provider 的职责、特征清单、配置方式。

- [ ] **Step 3: 更新 docs/codebase-review.md**

记录：
- 职责变化：`FeatureEngineer` → `FeatureHub` + 6 Providers
- 清理清单：engineer.py 已删除
- 本次改动如何减少边界泄漏：特征计算从单文件 God class 拆分为 6 个职责清晰的 Provider

- [ ] **Step 4: 提交**

```bash
git add CLAUDE.md docs/research-system.md docs/codebase-review.md
git commit -m "docs: update documentation for Feature Provider architecture"
```
