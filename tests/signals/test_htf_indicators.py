"""HTF 指标上下文注入功能测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.signals.models import SignalContext, SignalDecision
from src.signals.orchestration import SignalPolicy, SignalRuntime, SignalTarget


# ─── Test Helpers ──────────────────────────────────────────────────────


class DummySnapshotSource:
    """模拟 IndicatorManager，支持 get_indicator 查询。"""

    def __init__(self, indicator_data: dict | None = None):
        self.snapshot_listeners: list = []
        # indicator_data: {(symbol, tf, ind_name): {field: value}}
        self._indicator_data: dict = indicator_data or {}

    def add_snapshot_listener(self, listener):
        self.snapshot_listeners.append(listener)

    def remove_snapshot_listener(self, listener):
        self.snapshot_listeners = [
            item for item in self.snapshot_listeners if item is not listener
        ]

    def get_indicator(self, symbol: str, timeframe: str, indicator_name: str):
        return self._indicator_data.get((symbol, timeframe, indicator_name))

    def publish(self, symbol, timeframe, bar_time, indicators, scope="confirmed"):
        for listener in list(self.snapshot_listeners):
            listener(symbol, timeframe, bar_time, indicators, scope)


class DummySignalService:
    """记录 evaluate 调用，包括 htf_indicators 参数。"""

    def __init__(self):
        self.evaluate_calls: list[dict] = []

    def strategy_requirements(self, strategy: str):
        mapping = {
            "trend_htf": ("ema50",),
            "plain_trend": ("ema50",),
        }
        return mapping.get(strategy, ())

    def strategy_scopes(self, strategy: str):
        return ("confirmed",)

    def strategy_affinity_map(self, strategy: str):
        return {}

    def evaluate(self, **kwargs):
        self.evaluate_calls.append(kwargs)
        return SignalDecision(
            strategy=kwargs["strategy"],
            symbol=kwargs["symbol"],
            timeframe=kwargs["timeframe"],
            action="hold",
            confidence=0.3,
            reason="test",
        )

    def persist_decision(self, decision, indicators, metadata=None):
        pass

    soft_regime_enabled = False


def _make_runtime(
    service=None,
    source=None,
    targets=None,
    htf_indicators_enabled=True,
    htf_target_config=None,
):
    service = service or DummySignalService()
    source = source or DummySnapshotSource()
    targets = targets or [
        SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="trend_htf"),
        SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="plain_trend"),
        SignalTarget(symbol="XAUUSD", timeframe="H1", strategy="trend_htf"),
    ]
    if htf_target_config is None:
        # Default: trend_htf strategy needs adx14+ema50 from H1
        htf_target_config = {
            "trend_htf.adx14": "H1",
            "trend_htf.ema50": "H1",
        }
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=targets,
        enable_confirmed_snapshot=True,
        policy=SignalPolicy(),
        htf_indicators_enabled=htf_indicators_enabled,
        htf_target_config=htf_target_config,
    )
    return runtime, service, source


# ─── Tests ─────────────────────────────────────────────────────────────


class TestSignalContextHTF:
    """SignalContext htf_indicators 字段向后兼容性。"""

    def test_default_empty(self):
        ctx = SignalContext(symbol="X", timeframe="M5", strategy="s")
        assert ctx.htf_indicators == {}

    def test_with_htf_data(self):
        htf = {"H1": {"adx14": {"adx": 28.5}}}
        ctx = SignalContext(
            symbol="X", timeframe="M5", strategy="s", htf_indicators=htf
        )
        assert ctx.htf_indicators["H1"]["adx14"]["adx"] == 28.5


class TestResolveHTFIndicators:
    """SignalRuntime._resolve_htf_indicators 解析逻辑。"""

    def test_normal_resolution(self):
        """当前 M5 → 下一级已配置 TF 是 H1 → 正常返回 H1 数据。"""
        source = DummySnapshotSource(
            indicator_data={
                ("XAUUSD", "H1", "adx14"): {"adx": 30.0, "plus_di": 22.0},
                ("XAUUSD", "H1", "ema50"): {"ema": 2650.0},
            }
        )
        runtime, _, _ = _make_runtime(source=source)
        result = runtime._resolve_htf_indicators(
            "XAUUSD", "M5", {"H1": ("adx14", "ema50")}
        )
        assert "H1" in result
        assert result["H1"]["adx14"]["adx"] == 30.0
        assert result["H1"]["ema50"]["ema"] == 2650.0

    def test_no_higher_tf_configured(self):
        """当前已是最高配置 TF → 无 HTF 可用 → 返回空。"""
        runtime, _, _ = _make_runtime()
        result = runtime._resolve_htf_indicators(
            "XAUUSD", "H1", {"H1": ("adx14",)}
        )
        assert result == {}

    def test_indicator_not_available(self):
        """指标在 IndicatorManager 中不存在 → 不注入该指标。"""
        source = DummySnapshotSource(
            indicator_data={("XAUUSD", "H1", "adx14"): {"adx": 30.0}}
        )
        runtime, _, _ = _make_runtime(source=source)
        result = runtime._resolve_htf_indicators(
            "XAUUSD", "M5", {"H1": ("adx14", "nonexistent")}
        )
        assert "H1" in result
        assert "adx14" in result["H1"]
        assert "nonexistent" not in result["H1"]

    def test_all_indicators_missing(self):
        """所有指标都不存在 → 该 TF 不出现在结果中。"""
        runtime, _, _ = _make_runtime(source=DummySnapshotSource())
        result = runtime._resolve_htf_indicators(
            "XAUUSD", "M5", {"H1": ("nonexistent",)}
        )
        assert result == {}


class TestHTFInjectionInEvaluation:
    """验证 _evaluate_strategies 将 HTF 数据注入 service.evaluate()。"""

    def test_htf_payload_passed_to_evaluate(self):
        """声明了 htf_indicators 的策略应收到 HTF 数据。"""
        source = DummySnapshotSource(
            indicator_data={
                ("XAUUSD", "H1", "adx14"): {"adx": 28.0},
                ("XAUUSD", "H1", "ema50"): {"ema": 2650.0},
            }
        )
        runtime, service, _ = _make_runtime(source=source)
        indicators = {
            "ema50": {"ema": 2640.0},
        }
        from src.signals.evaluation.regime import RegimeType

        runtime._evaluate_strategies(
            symbol="XAUUSD",
            timeframe="M5",
            scope="confirmed",
            indicators=indicators,
            regime=RegimeType.UNCERTAIN,
            regime_metadata={"_regime": "uncertain"},
            event_time=datetime.now(timezone.utc),
            bar_time=datetime.now(timezone.utc),
            active_sessions=[],
        )
        # trend_htf 应该收到 HTF 数据
        htf_calls = [
            c for c in service.evaluate_calls if c["strategy"] == "trend_htf"
        ]
        assert len(htf_calls) == 1
        htf_indicators = htf_calls[0].get("htf_indicators", {})
        assert "H1" in htf_indicators
        assert htf_indicators["H1"]["adx14"]["adx"] == 28.0

    def test_no_htf_for_plain_strategy(self):
        """未声明 htf_indicators 的策略应收到空 dict。"""
        runtime, service, _ = _make_runtime()
        indicators = {"ema50": {"ema": 2640.0}}
        from src.signals.evaluation.regime import RegimeType

        runtime._evaluate_strategies(
            symbol="XAUUSD",
            timeframe="M5",
            scope="confirmed",
            indicators=indicators,
            regime=RegimeType.UNCERTAIN,
            regime_metadata={"_regime": "uncertain"},
            event_time=datetime.now(timezone.utc),
            bar_time=datetime.now(timezone.utc),
            active_sessions=[],
        )
        plain_calls = [
            c for c in service.evaluate_calls if c["strategy"] == "plain_trend"
        ]
        assert len(plain_calls) == 1
        assert plain_calls[0].get("htf_indicators", {}) == {}


class TestHTFGlobalSwitch:
    """htf_indicators_enabled 全局开关。"""

    def test_disabled_skips_htf_resolution(self):
        """全局关闭时，即使策略声明了 htf_indicators 也不注入。"""
        source = DummySnapshotSource(
            indicator_data={("XAUUSD", "H1", "adx14"): {"adx": 30.0}}
        )
        runtime, service, _ = _make_runtime(
            source=source, htf_indicators_enabled=False
        )
        indicators = {"ema50": {"ema": 2640.0}}
        from src.signals.evaluation.regime import RegimeType

        runtime._evaluate_strategies(
            symbol="XAUUSD",
            timeframe="M5",
            scope="confirmed",
            indicators=indicators,
            regime=RegimeType.UNCERTAIN,
            regime_metadata={"_regime": "uncertain"},
            event_time=datetime.now(timezone.utc),
            bar_time=datetime.now(timezone.utc),
            active_sessions=[],
        )
        htf_calls = [
            c for c in service.evaluate_calls if c["strategy"] == "trend_htf"
        ]
        assert len(htf_calls) == 1
        assert htf_calls[0].get("htf_indicators", {}) == {}



class TestHTFINIConfig:
    """INI [strategy_htf] per-indicator 配置驱动 HTF 注入。"""

    def test_ini_config_injects_specified_indicators(self):
        source = DummySnapshotSource(
            indicator_data={
                ("XAUUSD", "H1", "adx14"): {"adx": 30.0},
                ("XAUUSD", "H1", "ema50"): {"ema": 2650.0},
            }
        )
        runtime, _, _ = _make_runtime(
            source=source,
            htf_target_config={
                "trend_htf.adx14": "H1",
                "trend_htf.ema50": "H1",
            },
        )
        result = runtime._resolve_htf_indicators(
            "XAUUSD", "M5", {"H1": ["adx14", "ema50"]}
        )
        assert "H1" in result
        assert result["H1"]["adx14"]["adx"] == 30.0
        assert result["H1"]["ema50"]["ema"] == 2650.0

    def test_unconfigured_strategy_gets_no_htf(self):
        runtime, _, _ = _make_runtime(htf_target_config={})
        assert runtime._strategy_htf_config == {}

    def test_multi_tf_per_strategy(self):
        source = DummySnapshotSource(
            indicator_data={
                ("XAUUSD", "M15", "rsi14"): {"rsi": 25.0},
                ("XAUUSD", "H1", "ema50"): {"ema": 2650.0},
            }
        )
        runtime, _, _ = _make_runtime(
            source=source,
            htf_target_config={
                "trend_htf.rsi14": "M15",
                "trend_htf.ema50": "H1",
            },
        )
        spec = runtime._strategy_htf_config.get("trend_htf", {})
        assert "M15" in spec
        assert "H1" in spec



class TestConfiguredTimeframes:
    """启动时 _configured_timeframes 缓存构建。"""

    def test_configured_timeframes_from_targets(self):
        targets = [
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="s"),
            SignalTarget(symbol="XAUUSD", timeframe="H1", strategy="s"),
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="s2"),
        ]
        runtime, _, _ = _make_runtime(targets=targets)
        assert runtime._configured_timeframes == frozenset({"M5", "H1"})
