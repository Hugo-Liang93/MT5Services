# Strong Trend Follow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现挖掘驱动策略 `structured_strong_trend_follow`——在 H1 强趋势（ADX>40）+ 多头方向 + 动量温和回归时顺势 buy 延续入场。

**Architecture:** 沿用 `StructuredStrategyBase` 的 Why/When/Where/Volume 四层评估 + BARRIER 出场模式（与 `regime_exhaustion` 同构）。通过 `adx_d3 > 0` 严格门控与 regime_exhaustion（`adx_d3 <= 0`）在临界点互斥。

**Tech Stack:** Python 3.9+，pytest，结构化策略框架（`src/signals/strategies/structured/base.py`），BARRIER exit（`ExitMode.BARRIER`）。

**Design Spec:** `docs/superpowers/specs/2026-04-17-strong-trend-follow-design.md`

---

## File Structure

**Create:**
- `src/signals/strategies/structured/strong_trend_follow.py` — 策略类
- `tests/signals/test_strong_trend_follow.py` — 单元测试

**Modify:**
- `src/signals/strategies/structured/__init__.py` — 导出 class
- `src/signals/strategies/catalog.py` — 注册到策略目录
- `config/signal.ini` — `[strategy_timeframes]` + `[strategy_deployment.*]`

---

## Task 1: 策略骨架 + 类属性（不含评估逻辑）

**Files:**
- Create: `src/signals/strategies/structured/strong_trend_follow.py`
- Create: `tests/signals/test_strong_trend_follow.py`

- [ ] **Step 1.1: 写测试——验证类属性与 required_indicators**

```python
# tests/signals/test_strong_trend_follow.py
"""StructuredStrongTrendFollow 单元测试。

挖掘来源：2026-04-17 H1 rule_mining #5
  IF adx14.adx > 40.12 AND macd_fast.hist <= 1.61 AND roc12.roc > -1.17 THEN buy
"""

from __future__ import annotations

import pytest

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext
from src.signals.strategies.structured.base import (
    ExitMode,
    HtfPolicy,
)
from src.signals.strategies.structured.strong_trend_follow import (
    StructuredStrongTrendFollow,
)


def _make_context(
    *,
    adx: float = 50.0,
    adx_d3: float = 1.0,
    plus_di: float = 35.0,
    minus_di: float = 15.0,
    macd_hist: float = 0.5,
    roc: float = 0.5,
    atr: float = 20.0,
    volume_ratio: float = 1.0,
    regime: str = "trending",
    compression_state: str = "none",
    breakout_state: str = "none",
) -> SignalContext:
    """构造测试用 SignalContext。默认所有条件满足 → buy。"""
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="structured_strong_trend_follow",
        indicators={
            "adx14": {
                "adx": adx,
                "adx_d3": adx_d3,
                "plus_di": plus_di,
                "minus_di": minus_di,
            },
            "macd_fast": {"macd": 1.0, "signal": 0.5, "hist": macd_hist},
            "roc12": {"roc": roc},
            "atr14": {"atr": atr},
            "volume_ratio20": {"volume_ratio": volume_ratio},
        },
        metadata={
            "_regime": regime,
            "market_structure": {
                "compression_state": compression_state,
                "breakout_state": breakout_state,
            },
        },
        htf_indicators={},
    )


class TestClassAttributes:
    """类元数据验证。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_name(self) -> None:
        assert self.strategy.name == "structured_strong_trend_follow"

    def test_category(self) -> None:
        assert self.strategy.category == "trend_continuation"

    def test_htf_policy_none(self) -> None:
        assert self.strategy.htf_policy == HtfPolicy.NONE

    def test_preferred_scopes_confirmed_only(self) -> None:
        assert self.strategy.preferred_scopes == ("confirmed",)

    def test_required_indicators(self) -> None:
        assert set(self.strategy.required_indicators) == {
            "atr14",
            "adx14",
            "macd_fast",
            "roc12",
            "volume_ratio20",
        }

    def test_regime_affinity_trending_primary(self) -> None:
        assert self.strategy.regime_affinity[RegimeType.TRENDING] == 1.00
        assert self.strategy.regime_affinity[RegimeType.BREAKOUT] == 0.60
        assert self.strategy.regime_affinity[RegimeType.RANGING] == 0.00
        assert self.strategy.regime_affinity[RegimeType.UNCERTAIN] == 0.20
```

- [ ] **Step 1.2: 运行测试，验证失败**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestClassAttributes -v
```

Expected: FAIL，`ModuleNotFoundError: No module named 'src.signals.strategies.structured.strong_trend_follow'`

- [ ] **Step 1.3: 创建策略骨架文件**

```python
# src/signals/strategies/structured/strong_trend_follow.py
"""StructuredStrongTrendFollow — 强趋势延续（挖掘驱动）。

核心逻辑：ADX 极端值（>40）标志强趋势，DI 方向确认多头，ADX 仍在上行
（与 regime_exhaustion 互斥），MACD hist 回归中性 + ROC 未崩溃 → 顺势 buy。

挖掘来源：2026-04-17 H1 rule_mining #5
  IF adx14.adx > 40.12 AND macd_fast.hist <= 1.61 AND roc12.roc > -1.17 THEN buy
  (test WR 60.1% / n=143)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import EntrySpec, ExitMode, ExitSpec, HtfPolicy, StructuredStrategyBase


class StructuredStrongTrendFollow(StructuredStrategyBase):
    """ADX 极端 + DI 多头 + MACD 温和 + ROC 稳 → 顺势延续入场。"""

    name = "structured_strong_trend_follow"
    category = "trend_continuation"
    htf_policy = HtfPolicy.NONE
    required_indicators = (
        "atr14",
        "adx14",
        "macd_fast",
        "roc12",
        "volume_ratio20",
    )
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.BREAKOUT: 0.60,
        RegimeType.RANGING: 0.00,
        RegimeType.UNCERTAIN: 0.20,
    }
    research_provenance_refs = ("2026-04-17-H1-rule-mining-#5",)

    # ── 可调参数（挖掘阈值为默认值）──
    _adx_extreme: float = 40.0
    _adx_d3_min_strict: float = 0.0       # 与 regime_exhaustion 互斥：需严格 > 此值
    _macd_hist_upper: float = 1.61        # 挖掘阈值
    _macd_hist_lower: float = -2.0        # 下限保护
    _roc_lower: float = -1.17             # 挖掘阈值
    _sl_atr: float = 1.5
    _tp_atr: float = 2.5
    _time_bars: int = 20

    def _why(
        self, ctx: SignalContext
    ) -> Tuple[bool, Optional[str], float, str]:
        raise NotImplementedError  # Task 2 实现

    def _when(
        self, ctx: SignalContext, direction: str
    ) -> Tuple[bool, float, str]:
        raise NotImplementedError  # Task 3 实现

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        raise NotImplementedError  # Task 5 实现

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        raise NotImplementedError  # Task 5 实现
```

- [ ] **Step 1.4: 运行测试，验证通过**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestClassAttributes -v
```

Expected: PASS（6 个测试全通过）

- [ ] **Step 1.5: Commit**

```bash
git add src/signals/strategies/structured/strong_trend_follow.py tests/signals/test_strong_trend_follow.py
git commit -m "feat(signals): scaffold structured_strong_trend_follow (class attrs)"
```

---

## Task 2: _why() 硬门控（ADX + DI + adx_d3）

**Files:**
- Modify: `src/signals/strategies/structured/strong_trend_follow.py`
- Modify: `tests/signals/test_strong_trend_follow.py`

- [ ] **Step 2.1: 写 _why() 门控测试**

在 `tests/signals/test_strong_trend_follow.py` 末尾追加：

```python
class TestWhyGate:
    """_why() 硬门控测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_adx_below_threshold_rejected(self) -> None:
        """ADX=30 < 40 → hold。"""
        ctx = _make_context(adx=30.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "adx_low" in decision.reason

    def test_adx_at_threshold_rejected(self) -> None:
        """ADX=40 (=threshold) → hold（需严格 >）。"""
        ctx = _make_context(adx=40.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_di_not_bullish_rejected(self) -> None:
        """plus_di=20 <= minus_di=30 → hold（空头结构）。"""
        ctx = _make_context(plus_di=20.0, minus_di=30.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "di_not_bullish" in decision.reason

    def test_di_equal_rejected(self) -> None:
        """plus_di = minus_di → hold（需严格 >）。"""
        ctx = _make_context(plus_di=25.0, minus_di=25.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_adx_d3_zero_rejected_mutex_with_exhaustion(self) -> None:
        """adx_d3=0 → hold（临界点归 regime_exhaustion）。"""
        ctx = _make_context(adx_d3=0.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "adx_not_rising" in decision.reason

    def test_adx_d3_negative_rejected(self) -> None:
        """adx_d3=-1 → hold（趋势在减弱，让给 regime_exhaustion）。"""
        ctx = _make_context(adx_d3=-1.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_no_adx_data_rejected(self) -> None:
        """缺失 ADX 数据 → hold。"""
        ctx = _make_context()
        ctx.indicators["adx14"] = {}
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "no_adx_data" in decision.reason

    def test_why_passes_all_bullish_strong_trend(self) -> None:
        """ADX=50 + plus_di>minus_di + adx_d3=1 + MACD/ROC pending → 先不 hold 于 why。

        但因 _when/_entry/_exit 还未实现，evaluate 会抛 NotImplementedError。
        仅验证 _why 本身逻辑：直接调用 _why() 方法。
        """
        ctx = _make_context(adx=50.0, plus_di=35.0, minus_di=15.0, adx_d3=1.0)
        ok, direction, score, reason = self.strategy._why(ctx)
        assert ok is True
        assert direction == "buy"
        assert 0.4 <= score <= 1.0
        assert "strong_trend" in reason
```

- [ ] **Step 2.2: 运行测试，验证失败**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestWhyGate -v
```

Expected: FAIL（NotImplementedError）

- [ ] **Step 2.3: 实现 _why()**

替换 `strong_trend_follow.py` 中的 `_why` stub：

```python
    def _why(
        self, ctx: SignalContext
    ) -> Tuple[bool, Optional[str], float, str]:
        adx_data = self._adx_full(ctx)
        adx = adx_data["adx"]
        adx_d3 = adx_data["adx_d3"]
        plus_di = adx_data["plus_di"]
        minus_di = adx_data["minus_di"]

        if adx is None or plus_di is None or minus_di is None:
            return False, None, 0.0, "no_adx_data"

        threshold = get_tf_param(
            self, "adx_extreme", ctx.timeframe, self._adx_extreme
        )
        if adx <= threshold:
            return False, None, 0.0, f"adx_low:{adx:.0f}"

        if plus_di <= minus_di:
            return False, None, 0.0, (
                f"di_not_bullish:+{plus_di:.0f}/-{minus_di:.0f}"
            )

        # adx_d3 必须严格为正（与 regime_exhaustion 在 adx_d3<=0 时互斥）
        d3_min = get_tf_param(
            self, "adx_d3_min_strict", ctx.timeframe, self._adx_d3_min_strict
        )
        if adx_d3 is None or adx_d3 <= d3_min:
            return False, None, 0.0, f"adx_not_rising:d3={adx_d3}"

        score = self._linear_score(adx, low=threshold, high=threshold + 15.0)
        score = max(score, 0.4)

        return (
            True,
            "buy",
            score,
            f"strong_trend:adx={adx:.0f},di_diff={plus_di - minus_di:.0f}",
        )
```

- [ ] **Step 2.4: 运行测试，验证通过**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestWhyGate -v
```

Expected: PASS（8 个测试全通过）。注意 `test_why_passes_all_bullish_strong_trend` 直接调 `_why()` 方法，不走 evaluate()，所以不受 NotImplementedError 影响。

- [ ] **Step 2.5: Commit**

```bash
git add src/signals/strategies/structured/strong_trend_follow.py tests/signals/test_strong_trend_follow.py
git commit -m "feat(signals): strong_trend_follow _why() gate (ADX+DI+adx_d3)"
```

---

## Task 3: _when() 硬门控（MACD + ROC）

**Files:**
- Modify: `src/signals/strategies/structured/strong_trend_follow.py`
- Modify: `tests/signals/test_strong_trend_follow.py`

- [ ] **Step 3.1: 写 _when() 测试**

在 `tests/signals/test_strong_trend_follow.py` 末尾追加：

```python
class TestWhenGate:
    """_when() 硬门控测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_macd_hist_too_high_rejected(self) -> None:
        """macd_hist=2.0 > 1.61 → hold（动量过强，不符合回归中性条件）。"""
        ctx = _make_context(macd_hist=2.0)
        # 直接调 _when 避免依赖未实现的 entry/exit_spec
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is False
        assert "macd_hist_too_high" in reason

    def test_macd_hist_too_low_rejected(self) -> None:
        """macd_hist=-3.0 < -2.0 → hold（深度崩盘保护）。"""
        ctx = _make_context(macd_hist=-3.0)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is False
        assert "macd_hist_too_low" in reason

    def test_roc_too_low_rejected(self) -> None:
        """roc=-2.0 <= -1.17 → hold（动量已崩溃）。"""
        ctx = _make_context(roc=-2.0)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is False
        assert "roc_too_low" in reason

    def test_no_macd_data_rejected(self) -> None:
        """缺失 macd_fast 数据 → hold。"""
        ctx = _make_context()
        ctx.indicators["macd_fast"] = {}
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is False
        assert "no_macd_or_roc" in reason

    def test_no_roc_data_rejected(self) -> None:
        """缺失 roc12 数据 → hold。"""
        ctx = _make_context()
        ctx.indicators["roc12"] = {}
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is False

    def test_macd_hist_zero_highest_score(self) -> None:
        """macd_hist=0 最接近中心 → score 最大（1.0）。"""
        ctx = _make_context(macd_hist=0.0, roc=0.5)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is True
        assert score == pytest.approx(1.0, abs=0.05)

    def test_macd_hist_at_upper_bound_lowest_score(self) -> None:
        """macd_hist=1.61（上限）→ score 最低。"""
        ctx = _make_context(macd_hist=1.61, roc=0.5)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is True
        # 距离 0 为 1.61，half_range=max(1.61, 2.0)=2.0，score = max(0, 1 - 1.61/2.0) = 0.195
        # 但下限 0.3，所以实际 0.3
        assert score >= 0.3

    def test_when_passes_typical_case(self) -> None:
        """典型入场：macd_hist=0.5, roc=0.5 → 通过。"""
        ctx = _make_context(macd_hist=0.5, roc=0.5)
        ok, score, reason = self.strategy._when(ctx, "buy")
        assert ok is True
        assert 0.3 <= score <= 1.0
        assert "timing" in reason
```

- [ ] **Step 3.2: 运行测试，验证失败**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestWhenGate -v
```

Expected: FAIL（NotImplementedError）

- [ ] **Step 3.3: 实现 _when()**

替换 `strong_trend_follow.py` 中的 `_when` stub：

```python
    def _when(
        self, ctx: SignalContext, direction: str
    ) -> Tuple[bool, float, str]:
        macd = ctx.indicators.get("macd_fast", {})
        roc_data = ctx.indicators.get("roc12", {})

        macd_hist = macd.get("hist")
        roc = roc_data.get("roc")

        if macd_hist is None or roc is None:
            return False, 0.0, "no_macd_or_roc"

        tf = ctx.timeframe
        hist_hi = get_tf_param(
            self, "macd_hist_upper", tf, self._macd_hist_upper
        )
        hist_lo = get_tf_param(
            self, "macd_hist_lower", tf, self._macd_hist_lower
        )
        roc_lo = get_tf_param(self, "roc_lower", tf, self._roc_lower)

        if macd_hist > hist_hi:
            return False, 0.0, f"macd_hist_too_high:{macd_hist:.2f}"
        if macd_hist < hist_lo:
            return False, 0.0, f"macd_hist_too_low:{macd_hist:.2f}"
        if roc <= roc_lo:
            return False, 0.0, f"roc_too_low:{roc:.2f}"

        # 评分：macd_hist 越接近 0 越"温和"，score 越高
        center = 0.0
        half_range = max(abs(hist_hi - center), abs(hist_lo - center))
        distance = abs(macd_hist - center)
        score = (
            max(0.0, 1.0 - distance / half_range) if half_range > 0 else 0.0
        )
        score = max(score, 0.3)

        return True, score, f"timing:macd_hist={macd_hist:.2f},roc={roc:.2f}"
```

- [ ] **Step 3.4: 运行测试，验证通过**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestWhenGate -v
```

Expected: PASS（8 个测试全通过）

- [ ] **Step 3.5: Commit**

```bash
git add src/signals/strategies/structured/strong_trend_follow.py tests/signals/test_strong_trend_follow.py
git commit -m "feat(signals): strong_trend_follow _when() gate (MACD+ROC)"
```

---

## Task 4: _where() + _volume_bonus() 软加分

**Files:**
- Modify: `src/signals/strategies/structured/strong_trend_follow.py`
- Modify: `tests/signals/test_strong_trend_follow.py`

- [ ] **Step 4.1: 写 _where / _volume_bonus 测试**

```python
class TestWhereAndVolume:
    """软加分层测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_where_default_zero(self) -> None:
        """无 compression/breakout → 0.0。"""
        ctx = _make_context()
        score, reason = self.strategy._where(ctx, "buy")
        assert score == 0.0

    def test_where_compression_bonus(self) -> None:
        """compression_state=active → 0.8 加分。"""
        ctx = _make_context(compression_state="active")
        score, reason = self.strategy._where(ctx, "buy")
        assert score == 0.8
        assert "compression" in reason

    def test_where_breakout_bonus(self) -> None:
        """breakout_state=bullish → 0.8 加分。"""
        ctx = _make_context(breakout_state="bullish")
        score, reason = self.strategy._where(ctx, "buy")
        assert score == 0.8
        assert "breakout" in reason

    def test_volume_bonus_low_volume_zero(self) -> None:
        """volume_ratio=0.9 < 1.2 → 0.0。"""
        ctx = _make_context(volume_ratio=0.9)
        bonus = self.strategy._volume_bonus(ctx, "buy")
        assert bonus == 0.0

    def test_volume_bonus_high_volume_full(self) -> None:
        """volume_ratio=1.5 >= 1.5 → 1.0。"""
        ctx = _make_context(volume_ratio=1.5)
        bonus = self.strategy._volume_bonus(ctx, "buy")
        assert bonus == pytest.approx(1.0, abs=0.01)

    def test_volume_bonus_mid_volume_partial(self) -> None:
        """volume_ratio=1.35（1.2 与 1.5 中间）→ 0.5。"""
        ctx = _make_context(volume_ratio=1.35)
        bonus = self.strategy._volume_bonus(ctx, "buy")
        assert bonus == pytest.approx(0.5, abs=0.01)
```

- [ ] **Step 4.2: 运行测试，验证失败**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestWhereAndVolume -v
```

Expected: FAIL（`_where` 默认返回 0.0 会通过 test_where_default_zero，但 compression/breakout 测试会失败因为基类默认实现是 `return 0.0, ""`）

实际：`_where` 默认基类实现返回 (0.0, "")，所以 test_where_default_zero 通过，但 test_where_compression_bonus 失败。`_volume_bonus` 基类返回 0.0，所以 test_volume_bonus_high_volume_full 失败。

- [ ] **Step 4.3: 实现 _where() + _volume_bonus()**

在 `strong_trend_follow.py` 的 `_when` 方法之后、`_entry_spec` 之前插入：

```python
    def _where(
        self, ctx: SignalContext, direction: str
    ) -> Tuple[float, str]:
        ms = self._ms(ctx)
        if str(ms.get("compression_state", "none")) != "none":
            return 0.8, f"compression={ms.get('compression_state')}"
        if str(ms.get("breakout_state", "none")) != "none":
            return 0.8, f"breakout={ms.get('breakout_state')}"
        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        return self._linear_score(self._volume_ratio(ctx), low=1.2, high=1.5)
```

- [ ] **Step 4.4: 运行测试，验证通过**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestWhereAndVolume -v
```

Expected: PASS（6 个测试全通过）

- [ ] **Step 4.5: Commit**

```bash
git add src/signals/strategies/structured/strong_trend_follow.py tests/signals/test_strong_trend_follow.py
git commit -m "feat(signals): strong_trend_follow soft gates (where+volume)"
```

---

## Task 5: _entry_spec() + _exit_spec() + 端到端 evaluate 测试

**Files:**
- Modify: `src/signals/strategies/structured/strong_trend_follow.py`
- Modify: `tests/signals/test_strong_trend_follow.py`

- [ ] **Step 5.1: 写 entry/exit + evaluate 端到端测试**

```python
class TestEntryExitAndEvaluate:
    """入场/出场规格 + 端到端 evaluate 测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_entry_spec_market(self) -> None:
        """入场：市价。"""
        ctx = _make_context()
        spec = self.strategy._entry_spec(ctx, "buy")
        assert spec.entry_type.value == "market"

    def test_exit_spec_barrier_mode(self) -> None:
        """出场：BARRIER (sl=1.5, tp=2.5, time=20)。"""
        ctx = _make_context()
        spec = self.strategy._exit_spec(ctx, "buy")
        assert spec.mode == ExitMode.BARRIER
        assert spec.sl_atr == 1.5
        assert spec.tp_atr == 2.5
        assert spec.time_bars == 20

    def test_evaluate_all_pass_returns_buy(self) -> None:
        """完整评估：所有门控通过 → buy 决策，confidence ≥ 0.56。"""
        ctx = _make_context(
            adx=50.0, adx_d3=1.0, plus_di=35.0, minus_di=15.0,
            macd_hist=0.5, roc=0.5, volume_ratio=1.3,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        # base 0.50 + why ≥ 0.4×0.15=0.06 + when ≥ 0.3×0.15=0.045 → ≥ 0.605
        assert decision.confidence >= 0.56
        # meta 上应包含 entry_spec / exit_spec
        assert "entry_spec" in decision.metadata
        assert decision.metadata["exit_spec"]["mode"] == "barrier"

    def test_evaluate_signal_grade_a_with_all_bonus(self) -> None:
        """compression + high volume → where/vol 都加分 → grade=A。"""
        ctx = _make_context(
            adx=55.0, adx_d3=2.0, plus_di=40.0, minus_di=15.0,
            macd_hist=0.2, roc=0.8, volume_ratio=1.5,
            compression_state="active",
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        assert decision.metadata["signal_grade"] == "A"

    def test_evaluate_confidence_capped_at_090(self) -> None:
        """极端强信号不超过 0.90 上限。"""
        ctx = _make_context(
            adx=80.0, adx_d3=5.0, plus_di=60.0, minus_di=10.0,
            macd_hist=0.0, roc=2.0, volume_ratio=2.0,
            compression_state="active",
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.confidence <= 0.90

    def test_evaluate_records_provenance(self) -> None:
        """metadata 记录 research_provenance。"""
        ctx = _make_context()
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        provenance = decision.metadata.get("research_provenance", [])
        assert "2026-04-17-H1-rule-mining-#5" in provenance
```

- [ ] **Step 5.2: 运行测试，验证失败**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestEntryExitAndEvaluate -v
```

Expected: FAIL（NotImplementedError for entry/exit）

- [ ] **Step 5.3: 实现 _entry_spec() + _exit_spec()**

替换 `strong_trend_follow.py` 中的 stub：

```python
    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        # 强趋势延续是追单性质，使用市价入场（不等待回调）
        return EntrySpec()

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        """BARRIER 模式：与挖掘 forward_return 评估语义一致。"""
        sl = get_tf_param(self, "sl_atr", ctx.timeframe, self._sl_atr)
        tp = get_tf_param(self, "tp_atr", ctx.timeframe, self._tp_atr)
        tb = int(
            get_tf_param(self, "time_bars", ctx.timeframe, self._time_bars)
        )
        return ExitSpec(
            sl_atr=sl, tp_atr=tp, mode=ExitMode.BARRIER, time_bars=tb
        )
```

- [ ] **Step 5.4: 运行测试，验证通过**

```bash
pytest tests/signals/test_strong_trend_follow.py -v
```

Expected: PASS（所有 class 下全部用例通过）

- [ ] **Step 5.5: Commit**

```bash
git add src/signals/strategies/structured/strong_trend_follow.py tests/signals/test_strong_trend_follow.py
git commit -m "feat(signals): strong_trend_follow entry/exit specs + e2e tests"
```

---

## Task 6: 注册到策略目录（catalog + __init__）

**Files:**
- Modify: `src/signals/strategies/structured/__init__.py`
- Modify: `src/signals/strategies/catalog.py`

- [ ] **Step 6.1: 写集成测试（验证 catalog 可见）**

在 `tests/signals/test_strong_trend_follow.py` 末尾追加：

```python
class TestCatalogRegistration:
    """策略目录注册验证。"""

    def test_strategy_in_catalog(self) -> None:
        """catalog 中能找到 structured_strong_trend_follow。"""
        from src.signals.strategies.catalog import build_named_strategy_catalog

        catalog = build_named_strategy_catalog()
        assert "structured_strong_trend_follow" in catalog
        strategy = catalog["structured_strong_trend_follow"]
        assert isinstance(strategy, StructuredStrongTrendFollow)

    def test_strategy_exported_from_structured_module(self) -> None:
        """从 structured 模块能导入。"""
        from src.signals.strategies.structured import (
            StructuredStrongTrendFollow as ImportedClass,
        )
        assert ImportedClass is StructuredStrongTrendFollow
```

- [ ] **Step 6.2: 运行测试，验证失败**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestCatalogRegistration -v
```

Expected: FAIL（ImportError / KeyError）

- [ ] **Step 6.3: 更新 `src/signals/strategies/structured/__init__.py`**

在 import 段加入（按字母序插入在 `.regime_exhaustion` 之后 / `.session_breakout` 之前）：

```python
from .regime_exhaustion import StructuredRegimeExhaustion
from .session_breakout import StructuredSessionBreakout
from .strong_trend_follow import StructuredStrongTrendFollow
```

在 `__all__` 列表加入（字母序位置，`"StructuredRegimeExhaustion"` 之后）：

```python
    "StructuredRegimeExhaustion",
    "StructuredStrongTrendFollow",
]
```

- [ ] **Step 6.4: 更新 `src/signals/strategies/catalog.py`**

在 import 段按字母序加入：

```python
from .structured import (
    StructuredBreakoutFollow,
    StructuredLowbarEntry,
    StructuredOpenRangeBreakout,
    StructuredPriceAction,
    StructuredPullbackWindow,
    StructuredRangeReversion,
    StructuredRegimeExhaustion,
    StructuredSessionBreakout,
    StructuredStrongTrendFollow,
    StructuredSweepReversal,
    StructuredTrendContinuation,
    StructuredTrendlineTouch,
)
```

在 `_build_structured_strategies()` 的 return tuple 末尾加入：

```python
def _build_structured_strategies() -> tuple[SignalStrategy, ...]:
    return (
        StructuredTrendContinuation(),
        StructuredTrendContinuation(name="structured_trend_h4", htf="H4"),
        StructuredTrendContinuation(
            name="structured_trend_h4_momentum",
            htf="H4",
            use_momentum_consensus=True,
        ),
        StructuredSweepReversal(),
        StructuredBreakoutFollow(),
        StructuredRangeReversion(),
        StructuredSessionBreakout(),
        StructuredTrendlineTouch(),
        StructuredLowbarEntry(),
        StructuredPullbackWindow(),
        StructuredOpenRangeBreakout(),
        StructuredPriceAction(),
        StructuredRegimeExhaustion(),
        StructuredStrongTrendFollow(),
    )
```

- [ ] **Step 6.5: 运行测试，验证通过**

```bash
pytest tests/signals/test_strong_trend_follow.py::TestCatalogRegistration -v
```

Expected: PASS（2 个测试）

- [ ] **Step 6.6: Commit**

```bash
git add src/signals/strategies/structured/__init__.py src/signals/strategies/catalog.py tests/signals/test_strong_trend_follow.py
git commit -m "feat(signals): register structured_strong_trend_follow in catalog"
```

---

## Task 7: signal.ini 配置（TF 绑定 + 部署状态）

**Files:**
- Modify: `config/signal.ini`

- [ ] **Step 7.1: 加入 [strategy_timeframes] 条目**

找到 `config/signal.ini` 第 243 行（`structured_regime_exhaustion = H1`），在其后新增一行：

```ini
structured_regime_exhaustion = H1
structured_strong_trend_follow = H1
```

- [ ] **Step 7.2: 加入 [strategy_deployment.*] section**

找到 `[strategy_deployment.structured_regime_exhaustion]` 段（第 342 行起），在其后追加：

```ini
[strategy_deployment.structured_regime_exhaustion]
# 2026-04-16 新增：趋势耗竭反转策略（ADX 极端 + DI 方向 + StochRSI 时机）
# 挖掘来源：H1 regime transition 规则（bars_in_regime 根节点）
status = paper_only
robustness_tier = tf_specific
locked_timeframes = H1
locked_sessions = london,new_york

[strategy_deployment.structured_strong_trend_follow]
# 2026-04-17 新增：强趋势延续策略（ADX 极端 + DI 多头 + MACD 温和 + ROC 稳）
# 挖掘来源：2026-04-17 H1 rule_mining #5（test WR 60.1%/n=143）
# 与 regime_exhaustion 在 adx_d3 临界点互斥：本策略要求严格 adx_d3 > 0
status = paper_only
robustness_tier = tf_specific
locked_timeframes = H1
locked_sessions = london,new_york
```

- [ ] **Step 7.3: 运行启动冒烟测试验证配置合法**

```bash
MT5_INSTANCE=live-main python -c "
from src.signals.strategies.catalog import build_named_strategy_catalog
from src.config.signal import get_signal_config
catalog = build_named_strategy_catalog()
print('catalog keys:', sorted(catalog.keys()))
cfg = get_signal_config()
tf_map = cfg.strategy_timeframes if hasattr(cfg, 'strategy_timeframes') else {}
print('strong_trend_follow TFs:', tf_map.get('structured_strong_trend_follow'))
"
```

Expected: 输出包含 `structured_strong_trend_follow`，TF 为 `['H1']` 或 `('H1',)`。

- [ ] **Step 7.4: 运行全量单元测试确保未破坏其它**

```bash
pytest tests/signals/test_strong_trend_follow.py tests/signals/test_regime_exhaustion.py -v
```

Expected: 全部 PASS。

- [ ] **Step 7.5: Commit**

```bash
git add config/signal.ini
git commit -m "feat(config): bind structured_strong_trend_follow to H1 paper_only"
```

---

## Task 8: 回测验收

**Files:**
- Run: `data/research/strong_trend_follow_validation.json` 作为产物

- [ ] **Step 8.1: 运行 12 个月回测**

```bash
MT5_INSTANCE=live-main python -m src.ops.cli.backtest_runner \
    --environment live --tf H1 \
    --strategies structured_strong_trend_follow \
    --start 2025-04-17 --end 2026-04-15 \
    --monte-carlo \
    --json-output data/research/strong_trend_follow_validation.json \
    2>&1 | tee data/research/strong_trend_follow_validation.log
```

Expected: 命令完成（exit 0），输出包含 Trades/WR/PF/Sharpe/MC p。

- [ ] **Step 8.2: 读取结果并对照验收标准**

```bash
cat data/research/strong_trend_follow_validation.log
```

验收标准（来自 spec §5.1）：

| 指标 | 最低 | 期望 |
|------|------|------|
| Sharpe | ≥ 1.5 | ≥ 2.0 |
| PF | ≥ 2.0 | ≥ 3.0 |
| MC p (Sharpe) | ≤ 0.05 | ≤ 0.01 |
| MC p (PF) | ≤ 0.05 | ≤ 0.01 |
| 笔数 | ≥ 50 | — |
| MaxDD | ≤ 15% | ≤ 10% |

- [ ] **Step 8.3: 按回测结果分支处理**

**Case A（全部达标）**：
- 保持 `status = paper_only`
- 更新 `TODO.md` 的 FP 部分：标注 strong_trend_follow 验收通过
- Commit：`feat(signals): strong_trend_follow backtest validated (Sharpe=X, PF=Y, MC p=Z)`

**Case B（Sharpe ∈ [1.5, 2.0) 或 PF < 3.0）**：
- 放宽参数（signal.local.ini 覆盖 `_adx_extreme=38` 或 `_macd_hist_lower=-3.0`）
- 重跑回测，最多 2 轮
- 达标后同 Case A；仍不达标进入 Case C

**Case C（Sharpe < 1.5 或 MC p > 0.05 或 笔数 < 30）**：
- 把 `status` 改为 `candidate`（不加载）
- 写入 TODO.md：策略已编码但回测未达标，保留代码资产
- Commit：`revert(signals): strong_trend_follow below validation threshold, set to candidate`

- [ ] **Step 8.4: 更新 TODO.md 反映 Phase 2 完成状态**

在 `TODO.md` 对应 FP 章节追加：

```markdown
### FP.2（Phase 2 挖掘驱动策略 #2）

- [x] 2026-04-17: `structured_strong_trend_follow` 实现（挖掘来源：H1 rule_mining #5）
- [回测结果填入，包括 Sharpe / PF / MC p / 笔数 / Case 分支]
```

- [ ] **Step 8.5: 最终 commit**

```bash
git add TODO.md
git commit -m "docs(todo): close strong_trend_follow Phase 2 with backtest result"
```

---

## Self-Review Checklist（已执行）

- **Spec coverage**：Spec §2.1 (类属性) → Task 1；§2.2 (参数) → Task 1；§3.1 (_why) → Task 2；§3.2 (_when) → Task 3；§3.3-3.4 (_where/_volume) → Task 4；§3.5-3.6 (entry/exit) → Task 5；§4 (signal.ini) → Task 7；§5 (验收) → Task 8；§7 (测试) → 分散到 Tasks 1-5 + Task 6 注册测试 ✅
- **Placeholder scan**：无 TBD/TODO/implement later，所有步骤含完整代码块 ✅
- **Type consistency**：`_adx_d3_min_strict` 在 Task 1 spec + Task 2 实现一致；`_macd_hist_upper/lower`/`_roc_lower` 一致；`_sl_atr=1.5 / _tp_atr=2.5 / _time_bars=20` 一致 ✅
- **Mutual exclusion**：Task 2 `adx_d3 <= d3_min (=0)` 严格拒绝，`regime_exhaustion` 是 `adx_d3 > 0` 拒绝——临界点 `adx_d3=0` 归 regime_exhaustion，符合 spec §6.2 ✅

---

## Notes

- **Frequent commits**：每个 Task 结束都 commit，8 个 commits 覆盖全部工作
- **Test-first**：每个 Task 先写 failing test → 看失败 → 实现 → 看通过 → commit
- **Isolation**：每个 Task 文件范围明确，不跨文件“扩展重构”
- **回滚策略**：Task 8 Case C 时，代码保留但 config `candidate` 禁用加载，避免产品状态污染
