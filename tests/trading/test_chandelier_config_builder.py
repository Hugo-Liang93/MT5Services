"""build_chandelier_config 单一构建入口测试。

设计原则：
- exit_rules 模块持有所有出场配置构建职责（DRY + 职责边界）
- factories/signals 与 backtesting/engine 共用同一构建入口避免参数构造漂移
- duck typing 接受任何带 chandelier_* 字段的对象（SignalConfig / 未来 ExitConfig）
"""
from __future__ import annotations

from types import SimpleNamespace

from src.trading.positions.exit_rules import (
    ChandelierConfig,
    build_chandelier_config,
    profile_from_aggression,
)


def _stub_signal_config(**overrides) -> SimpleNamespace:
    """模拟 SignalConfig 中 chandelier 相关字段的最小集。"""
    base = dict(
        chandelier_regime_aware=True,
        chandelier_default_alpha=0.50,
        chandelier_breakeven_enabled=True,
        chandelier_breakeven_buffer_r=0.1,
        chandelier_min_breakeven_buffer=1.0,
        chandelier_signal_exit_enabled=True,
        chandelier_signal_exit_confirmation_bars=2,
        chandelier_timeout_bars=0,
        chandelier_max_tp_r=5.0,
        chandelier_enforce_r_floor=True,
        chandelier_aggression_overrides={("trend", "trending"): 0.85},
        chandelier_tf_trail_scale={"M5": 1.20, "H1": 1.00},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_chandelier_config_maps_all_fields() -> None:
    """所有 chandelier_* 字段都正确映射到 ChandelierConfig。"""
    cfg = build_chandelier_config(_stub_signal_config())

    assert isinstance(cfg, ChandelierConfig)
    assert cfg.regime_aware is True
    # default_profile 由 default_alpha 经 profile_from_aggression 推导
    assert cfg.default_profile == profile_from_aggression(0.50)
    assert cfg.breakeven_enabled is True
    assert cfg.breakeven_buffer_r == 0.1
    assert cfg.min_breakeven_buffer == 1.0
    assert cfg.signal_exit_enabled is True
    assert cfg.signal_exit_confirmation_bars == 2
    assert cfg.timeout_bars == 0
    assert cfg.max_tp_r == 5.0
    assert cfg.enforce_r_floor is True
    assert cfg.aggression_overrides == {("trend", "trending"): 0.85}
    assert cfg.tf_trail_scale == {"M5": 1.20, "H1": 1.00}


def test_build_chandelier_config_copies_mappings_defensively() -> None:
    """aggression_overrides / tf_trail_scale 必须是新 dict 副本，避免外部修改污染 cfg。"""
    overrides = {("trend", "trending"): 0.85}
    tf_scale = {"M5": 1.20}
    src = _stub_signal_config(
        chandelier_aggression_overrides=overrides,
        chandelier_tf_trail_scale=tf_scale,
    )

    cfg = build_chandelier_config(src)

    # 修改源 dict 不应影响已构建的 cfg
    overrides[("reversion", "ranging")] = 0.99
    tf_scale["D1"] = 0.5
    assert ("reversion", "ranging") not in cfg.aggression_overrides
    assert "D1" not in cfg.tf_trail_scale


def test_build_chandelier_config_reflects_alpha_change() -> None:
    """default_alpha 改变应通过 profile_from_aggression 重新派生 default_profile。"""
    cfg_low = build_chandelier_config(_stub_signal_config(chandelier_default_alpha=0.10))
    cfg_high = build_chandelier_config(_stub_signal_config(chandelier_default_alpha=0.90))

    # 低 alpha → 紧 trail（小 atr_multiplier）
    assert cfg_low.default_profile.chandelier_atr_multiplier < cfg_high.default_profile.chandelier_atr_multiplier
    # 低 alpha → 高锁利
    assert cfg_low.default_profile.lock_ratio > cfg_high.default_profile.lock_ratio


def test_build_chandelier_config_accepts_duck_typed_source() -> None:
    """source 不需要是 SignalConfig；任何带相同字段名的对象都可（duck typing）。

    这是有意设计：将来 chandelier 字段从 SignalConfig 拆出独立 ExitConfig
    时，无需修改本函数。
    """
    class DuckTypedExitConfig:
        chandelier_regime_aware = False
        chandelier_default_alpha = 0.30
        chandelier_breakeven_enabled = False
        chandelier_breakeven_buffer_r = 0.2
        chandelier_min_breakeven_buffer = 2.0
        chandelier_signal_exit_enabled = False
        chandelier_signal_exit_confirmation_bars = 5
        chandelier_timeout_bars = 100
        chandelier_max_tp_r = 8.0
        chandelier_enforce_r_floor = False
        chandelier_aggression_overrides = {}
        chandelier_tf_trail_scale = {}

    cfg = build_chandelier_config(DuckTypedExitConfig())

    assert cfg.regime_aware is False
    assert cfg.timeout_bars == 100
    assert cfg.max_tp_r == 8.0
    assert cfg.enforce_r_floor is False
