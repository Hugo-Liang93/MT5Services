"""M0.2 验证：AppRuntime._log_startup_summary() 产出结构化摘要。

目的不是测 start() 全链路，而是隔离验证：
  - 从 container.runtime_identity + signal_config_loader 能组装出预期字段
  - signal_config_loader 抛异常时降级为 "?"/[]/{}，不炸启动
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from src.app_runtime.container import AppContainer
from src.app_runtime.runtime import AppRuntime


def _build_runtime_with_identity(
    *,
    signal_config_loader=None,
    mode_value: str | None = "full",
) -> AppRuntime:
    container = AppContainer()
    container.runtime_identity = SimpleNamespace(
        instance_name="live-main",
        environment="live",
        instance_role="main",
    )
    if mode_value is not None:
        container.runtime_mode_controller = SimpleNamespace(
            current_mode=lambda: SimpleNamespace(value=mode_value)
        )
    return AppRuntime(container, signal_config_loader=signal_config_loader)


def test_startup_summary_emits_identity_and_mode(caplog):
    rt = _build_runtime_with_identity()
    with caplog.at_level(logging.INFO, logger="src.app_runtime.runtime"):
        rt._log_startup_summary()

    records = [r.getMessage() for r in caplog.records if "startup_summary" in r.getMessage()]
    assert len(records) == 1
    msg = records[0]
    assert "instance=live-main" in msg
    assert "env=live" in msg
    assert "role=main" in msg
    assert "mode=full" in msg
    # 无 signal_config_loader → 字段降级
    assert "auto_trade=?" in msg
    assert "active_strategies=[]" in msg


def test_startup_summary_reads_signal_config(caplog):
    signal_cfg = SimpleNamespace(
        auto_trade_enabled=True,
        intrabar_trading_enabled=False,
        strategy_timeframes={
            "trend_continuation": ["H1"],
            "breakout_follow": ["M30"],
        },
        intrabar_trading_enabled_strategies=["breakout_follow"],
        account_bindings={"live-main": ["trend_continuation"]},
    )
    rt = _build_runtime_with_identity(signal_config_loader=lambda: signal_cfg)
    with caplog.at_level(logging.INFO, logger="src.app_runtime.runtime"):
        rt._log_startup_summary()

    msg = next(r.getMessage() for r in caplog.records if "startup_summary" in r.getMessage())
    assert "auto_trade=True" in msg
    assert "intrabar_enabled=False" in msg
    # sorted 保证可复现
    assert "active_strategies=['breakout_follow', 'trend_continuation']" in msg
    assert "intrabar_strategies=['breakout_follow']" in msg
    assert "account_bindings={'live-main': ['trend_continuation']}" in msg


def test_startup_summary_degrades_on_loader_failure(caplog):
    def _boom():
        raise RuntimeError("config blew up")

    rt = _build_runtime_with_identity(signal_config_loader=_boom)
    with caplog.at_level(logging.INFO, logger="src.app_runtime.runtime"):
        rt._log_startup_summary()  # 不应抛

    msg = next(r.getMessage() for r in caplog.records if "startup_summary" in r.getMessage())
    assert "auto_trade=?" in msg
    assert "active_strategies=[]" in msg
    assert "account_bindings={}" in msg


def test_startup_summary_degrades_on_mode_failure(caplog):
    container = AppContainer()
    container.runtime_identity = SimpleNamespace(
        instance_name="live-main",
        environment="live",
        instance_role="main",
    )

    class _BrokenController:
        def current_mode(self):
            raise RuntimeError("mode controller offline")

    container.runtime_mode_controller = _BrokenController()
    rt = AppRuntime(container)
    with caplog.at_level(logging.INFO, logger="src.app_runtime.runtime"):
        rt._log_startup_summary()

    msg = next(r.getMessage() for r in caplog.records if "startup_summary" in r.getMessage())
    assert "mode=error" in msg
