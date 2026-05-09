from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace

import pytest

from src.ops import mt5_session_gate
from src.ops.cli import live_preflight
from src.signals.contracts import StrategyDeployment, StrategyDeploymentStatus


def test_ensure_mt5_session_gate_or_raise_surfaces_error_code(monkeypatch) -> None:
    settings = SimpleNamespace(instance_name="live-main", account_alias="live_main")
    state = SimpleNamespace(
        session_ready=False,
        error_code="interactive_login_required",
        error_message="terminal needs manual unlock/login",
    )

    monkeypatch.setattr(
        mt5_session_gate,
        "probe_mt5_session_gate",
        lambda instance_name=None, auto_launch_terminal=True: (settings, state),
    )

    with pytest.raises(RuntimeError, match="interactive_login_required"):
        mt5_session_gate.ensure_mt5_session_gate_or_raise(instance_name="live-main")


def test_ensure_topology_group_mt5_session_gate_or_raise_validates_all_instances(
    monkeypatch,
) -> None:
    group = SimpleNamespace(
        name="live",
        main="live-main",
        workers=["live-exec-a"],
        environment="live",
    )

    monkeypatch.setattr(
        "src.config.topology.load_topology_group", lambda group_name: group
    )
    monkeypatch.setattr(
        mt5_session_gate,
        "ensure_mt5_session_gate_or_raise",
        lambda instance_name=None, auto_launch_terminal=True: (
            SimpleNamespace(account_alias=instance_name, instance_name=instance_name),
            SimpleNamespace(
                to_dict=lambda: {"session_ready": True, "error_code": None}
            ),
        ),
    )

    payload = mt5_session_gate.ensure_topology_group_mt5_session_gate_or_raise("live")

    assert sorted(payload.keys()) == ["live-exec-a", "live-main"]
    assert payload["live-main"]["state"]["session_ready"] is True


def test_check_mt5_reports_interactive_login_required(monkeypatch) -> None:
    fake_settings = SimpleNamespace(
        mt5_path="C:/Program Files/TradeMax Global MT5 Terminal/terminal64.exe",
        mt5_server="TradeMaxGlobal-Live",
        mt5_login=60067107,
        mt5_password="secret",
        timezone="UTC",
        server_time_offset_hours=None,
    )
    fake_state = SimpleNamespace(
        terminal_reachable=True,
        terminal_process_ready=True,
        ipc_ready=False,
        authorized=False,
        account_match=False,
        session_ready=False,
        interactive_login_required=True,
        error_code="interactive_login_required",
        error_message="terminal needs manual unlock/login",
    )

    monkeypatch.setattr(
        "src.config.mt5.load_mt5_settings",
        lambda instance_name=None: fake_settings,
    )
    monkeypatch.setattr(
        "src.clients.base.MT5BaseClient.inspect_session_state",
        lambda self, **kwargs: fake_state,
    )
    monkeypatch.setattr("src.clients.base.mt5", None)

    rows = live_preflight._check_mt5_instance()

    assert any(
        name == "MT5 session gate"
        and status == "FAIL"
        and "interactive_login_required" in detail
        for name, status, detail in rows
    )


def test_check_mt5_instance_defaults_to_strict_terminal_process(monkeypatch) -> None:
    fake_settings = SimpleNamespace(
        mt5_path="C:/Program Files/TMGM MT5 Terminal/terminal64.exe",
        mt5_server="TradeMaxGlobal-Demo",
        mt5_login=60067107,
        mt5_password="secret",
        timezone="UTC",
        server_time_offset_hours=None,
    )
    fake_state = SimpleNamespace(
        terminal_reachable=False,
        terminal_process_ready=False,
        ipc_ready=False,
        authorized=False,
        account_match=False,
        session_ready=False,
        interactive_login_required=False,
        error_code="terminal_not_running",
        error_message="MT5 terminal process is not running",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "src.config.mt5.load_mt5_settings",
        lambda instance_name=None: fake_settings,
    )

    def fake_inspect(self, **kwargs):
        captured.update(kwargs)
        return fake_state

    monkeypatch.setattr(
        "src.clients.base.MT5BaseClient.inspect_session_state",
        fake_inspect,
    )
    monkeypatch.setattr("src.clients.base.mt5", None)

    live_preflight._check_mt5_instance()

    assert captured["require_terminal_process"] is True
    assert captured["attempt_initialize"] is True
    assert captured["attempt_login"] is True


def test_check_mt5_instance_auto_launch_lets_mt5_initialize_terminal(
    monkeypatch,
) -> None:
    fake_settings = SimpleNamespace(
        mt5_path="C:/Program Files/TMGM MT5 Terminal/terminal64.exe",
        mt5_server="TradeMaxGlobal-Demo",
        mt5_login=60067107,
        mt5_password="secret",
        timezone="UTC",
        server_time_offset_hours=None,
    )
    fake_state = SimpleNamespace(
        terminal_reachable=True,
        terminal_process_ready=True,
        ipc_ready=True,
        authorized=True,
        account_match=True,
        session_ready=True,
        interactive_login_required=False,
        error_code=None,
        error_message=None,
        terminal_name="MetaTrader 5",
        login=60067107,
        server="TradeMaxGlobal-Demo",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "src.config.mt5.load_mt5_settings",
        lambda instance_name=None: fake_settings,
    )

    def fake_inspect(self, **kwargs):
        captured.update(kwargs)
        return fake_state

    monkeypatch.setattr(
        "src.clients.base.MT5BaseClient.inspect_session_state",
        fake_inspect,
    )
    monkeypatch.setattr("src.clients.base.mt5", None)

    rows = live_preflight._check_mt5_instance(auto_launch_terminal=True)

    assert captured["require_terminal_process"] is False
    assert any(
        name == "MT5 session gate" and status == "OK" for name, status, _ in rows
    )


def test_check_mt5_uses_environment_topology_group(monkeypatch) -> None:
    group = SimpleNamespace(main="live-main", workers=["live-exec-a"])
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        "src.config.topology.load_topology_group", lambda group_name: group
    )
    monkeypatch.setattr(
        live_preflight,
        "_check_mt5_instance",
        lambda instance_name=None, auto_launch_terminal=False: calls.append(
            (instance_name or "default", auto_launch_terminal)
        )
        or [],
    )

    live_preflight._check_mt5("live", auto_launch_terminal=True)

    assert calls == [("live-main", True), ("live-exec-a", True)]


# ---------------------------------------------------------------------------
# _check_api — regression: was probing removed /v1/health route → constant 404
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def test_check_api_hits_root_health_not_v1_health(monkeypatch) -> None:
    """回归：旧实现请求 /v1/health（已不存在），稳定 404 → preflight 误报 FAIL。

    当前路由树：根 /health（业务级 ApiResponse）+ /v1/monitoring/health{,/live,/ready}。
    preflight 应走 /health（root），它返回 ApiResponse[dict] 含 mode/market/trading。
    """
    captured: list[str] = []

    def fake_get(url: str, timeout: float = 5) -> _FakeResp:
        captured.append(url)
        return _FakeResp(
            200,
            {
                "success": True,
                "data": {"mode": "FULL", "market": {"connected": True}},
            },
        )

    import requests as _req  # imported inside _check_api; patch module attr

    monkeypatch.setattr(_req, "get", fake_get)

    rows = live_preflight._check_api(api_base="http://localhost:8808")

    assert captured == ["http://localhost:8808/health"], (
        "_check_api 必须请求 root /health（business-level ApiResponse），"
        f"非已删除的 /v1/health；实际请求 {captured!r}"
    )
    assert rows
    name, status, detail = rows[0]
    assert name == "API health"
    assert status == "OK"


def test_check_api_extracts_mode_from_response(monkeypatch) -> None:
    """回归：旧实现读 data.status（无该字段）始终输出 'unknown'。

    新实现应至少能反映 runtime mode（FULL/OBSERVE/RISK_OFF/INGEST_ONLY）。
    """
    import requests as _req

    monkeypatch.setattr(
        _req,
        "get",
        lambda url, timeout=5: _FakeResp(
            200,
            {
                "success": True,
                "data": {
                    "mode": "OBSERVE",
                    "market": {"connected": True},
                    "trading": {"running": False},
                },
            },
        ),
    )

    rows = live_preflight._check_api()
    _, status, detail = rows[0]
    assert status == "OK"
    assert (
        "OBSERVE" in detail
    ), f"detail 应反映 runtime mode，旧实现死写 'unknown'；实际 {detail!r}"


def test_check_api_handles_connection_error(monkeypatch) -> None:
    """ConnectionError → SKIP（service not yet running 是合法 preflight 场景）。"""
    import requests as _req

    def boom(url: str, timeout: float = 5):
        raise _req.ConnectionError("conn refused")

    monkeypatch.setattr(_req, "get", boom)

    rows = live_preflight._check_api()
    _, status, _ = rows[0]
    assert status == "SKIP"


def test_check_api_marks_failure_when_success_false(monkeypatch) -> None:
    """ApiResponse.success=False → FAIL（避免静默把异常状态当 OK）。"""
    import requests as _req

    monkeypatch.setattr(
        _req,
        "get",
        lambda url, timeout=5: _FakeResp(
            200,
            {"success": False, "error": {"code": "DEPS_NOT_READY"}},
        ),
    )

    rows = live_preflight._check_api()
    _, status, detail = rows[0]
    assert status == "FAIL"
    assert "DEPS_NOT_READY" in detail or "success=False" in detail


# ---------------------------------------------------------------------------
# P2 #5: live_preflight reads removed min_preview_confidence field
# ---------------------------------------------------------------------------


def test_live_preflight_reads_auto_trade_min_confidence_not_removed_field() -> None:
    """回归：旧实现 getattr(signal_cfg, 'min_preview_confidence', 0.55) →
    SignalConfig 已无此字段，永远 default 0.55；真实 live 阈值
    auto_trade_min_confidence 没被读 → preflight 实盘 vs 回测差异表伪事实。
    """
    import inspect as _inspect

    source = _inspect.getsource(live_preflight)
    # 禁止再读已删字段
    assert (
        "min_preview_confidence" not in source
    ), "SignalConfig 已无 min_preview_confidence；改用 auto_trade_min_confidence"
    # 必须读真实字段
    assert (
        "auto_trade_min_confidence" in source
    ), "preflight min_confidence 比较应基于 auto_trade_min_confidence（真实 live 阈值）"


# ---------------------------------------------------------------------------
# P3: MT5 symbol probe 不应硬编码 XAUUSD（SSOT 是 app.ini trading.*）
# ---------------------------------------------------------------------------


def test_live_preflight_symbol_probe_not_hardcoded_xauusd() -> None:
    """P3 回归：MT5 symbol probe 不应硬编码 "XAUUSD"。

    SSOT 是 config/app.ini trading.symbols / default_symbol。
    一旦实例改成别的 symbol 或多品种，硬编码会把 "XAUUSD 不在 Market Watch"
    误报为 preflight FAIL，不反映真实配置目标。
    """
    import inspect as _inspect

    source = _inspect.getsource(live_preflight)
    # 至少有一处从配置读 symbol
    assert any(
        token in source
        for token in (
            "get_trading_config",
            "get_shared_default_symbol",
            "get_symbols",
            "trading_config",
            ".default_symbol",
            ".symbols",
        )
    ), (
        "live_preflight 应从 trading config 读 symbols/default_symbol，"
        "不应硬编码 XAUUSD（SSOT 在 app.ini trading.*）"
    )
    # 主体 mt5.symbol_info(...) 不应再硬写 "XAUUSD"
    assert (
        'mt5.symbol_info("XAUUSD")' not in source
    ), "mt5.symbol_info 不应硬编码 XAUUSD；用配置值或循环遍历 trading.symbols"


# ---------------------------------------------------------------------------
# Effective signal routing — auto-trade 必须有可执行策略到账户的正式绑定
# ---------------------------------------------------------------------------


def _deployment(name: str, status: StrategyDeploymentStatus) -> StrategyDeployment:
    return StrategyDeployment(name=name, status=status)


def test_effective_strategy_routing_fails_when_auto_trade_has_no_bindings(
    monkeypatch,
) -> None:
    strategy_name = "structured_price_action"
    cfg = SimpleNamespace(
        auto_trade_enabled=True,
        strategy_deployments={
            strategy_name: _deployment(
                strategy_name, StrategyDeploymentStatus.DEMO_VALIDATION
            )
        },
        account_bindings={},
    )

    monkeypatch.setattr("src.config.signal.get_signal_config", lambda: cfg)
    monkeypatch.setattr(
        "src.signals.strategies.catalog.build_named_strategy_catalog",
        lambda: OrderedDict([(strategy_name, SimpleNamespace(name=strategy_name))]),
    )

    rows = live_preflight._check_effective_strategy_routing("demo")

    assert any(
        name == "Effective strategy routing"
        and status == "FAIL"
        and "auto_trade_enabled=true" in detail
        and strategy_name in detail
        for name, status, detail in rows
    )


def test_effective_strategy_routing_fails_on_unregistered_binding(monkeypatch) -> None:
    strategy_name = "structured_price_action"
    cfg = SimpleNamespace(
        auto_trade_enabled=True,
        strategy_deployments={
            strategy_name: _deployment(strategy_name, StrategyDeploymentStatus.ACTIVE)
        },
        account_bindings={"demo_main": ["ghost_strategy"]},
    )

    monkeypatch.setattr("src.config.signal.get_signal_config", lambda: cfg)
    monkeypatch.setattr(
        "src.signals.strategies.catalog.build_named_strategy_catalog",
        lambda: OrderedDict([(strategy_name, SimpleNamespace(name=strategy_name))]),
    )

    rows = live_preflight._check_effective_strategy_routing("demo")

    assert any(
        name == "Effective strategy routing"
        and status == "FAIL"
        and "unregistered" in detail
        and "ghost_strategy" in detail
        for name, status, detail in rows
    )


def test_effective_strategy_routing_blocks_non_executable_live_binding(
    monkeypatch,
) -> None:
    strategy_name = "structured_price_action"
    cfg = SimpleNamespace(
        auto_trade_enabled=True,
        strategy_deployments={
            strategy_name: _deployment(
                strategy_name, StrategyDeploymentStatus.DEMO_VALIDATION
            )
        },
        account_bindings={"live_main": [strategy_name]},
    )

    monkeypatch.setattr("src.config.signal.get_signal_config", lambda: cfg)
    monkeypatch.setattr(
        "src.signals.strategies.catalog.build_named_strategy_catalog",
        lambda: OrderedDict([(strategy_name, SimpleNamespace(name=strategy_name))]),
    )

    rows = live_preflight._check_effective_strategy_routing("live")

    assert any(
        name == "Effective strategy routing"
        and status == "FAIL"
        and "not executable in live" in detail
        and strategy_name in detail
        for name, status, detail in rows
    )


def test_effective_strategy_routing_ignores_other_environment_bindings(
    monkeypatch,
) -> None:
    strategy_name = "structured_price_action"
    cfg = SimpleNamespace(
        auto_trade_enabled=True,
        strategy_deployments={
            strategy_name: _deployment(
                strategy_name, StrategyDeploymentStatus.DEMO_VALIDATION
            )
        },
        account_bindings={"demo_main": [strategy_name]},
    )
    group = SimpleNamespace(main="live-main", workers=(), instances=("live-main",))

    monkeypatch.setattr("src.config.signal.get_signal_config", lambda: cfg)
    monkeypatch.setattr(
        "src.signals.strategies.catalog.build_named_strategy_catalog",
        lambda: OrderedDict([(strategy_name, SimpleNamespace(name=strategy_name))]),
    )
    monkeypatch.setattr(
        "src.config.topology.load_topology_group", lambda group_name: group
    )

    rows = live_preflight._check_effective_strategy_routing("live")

    assert rows == [
        (
            "Effective strategy routing",
            "OK",
            "auto_trade_enabled=true; no strategy executable in live",
        )
    ]


# ---------------------------------------------------------------------------
# Instance-scoped risk — live topology 必须审计实例 local 覆盖后的有效风控
# ---------------------------------------------------------------------------


def test_instance_risk_safety_blocks_live_topology_high_risk_overrides(
    monkeypatch,
) -> None:
    group = SimpleNamespace(main="live-main", workers=(), instances=("live-main",))
    high_risk = SimpleNamespace(
        max_positions_per_symbol=6,
        max_volume_per_order=0.01,
        max_volume_per_symbol=0.06,
        daily_loss_limit_pct=25.0,
        max_trades_per_day=5,
        data_unavailable_policy="fail_closed",
    )

    monkeypatch.setattr(
        "src.config.topology.load_topology_group", lambda group_name: group
    )
    monkeypatch.setattr(
        live_preflight,
        "_load_instance_risk_config",
        lambda instance_name: high_risk,
        raising=False,
    )

    rows = live_preflight._check_instance_risk_safety("live")

    assert any(
        name == "[live-main] Risk: daily_loss_limit_pct"
        and status == "FAIL"
        and "25.0%" in detail
        for name, status, detail in rows
    )
    assert any(
        name == "[live-main] Risk: max_positions_per_symbol"
        and status == "FAIL"
        and "6" in detail
        for name, status, detail in rows
    )
    assert any(
        name == "[live-main] Risk: max_volume_per_symbol"
        and status == "FAIL"
        and "0.06" in detail
        for name, status, detail in rows
    )


def test_instance_risk_safety_uses_configured_preflight_policy(
    monkeypatch,
) -> None:
    from src.config.models import PreflightRiskPolicy

    group = SimpleNamespace(main="live-main", workers=(), instances=("live-main",))
    policy = PreflightRiskPolicy(
        live_max_positions_per_symbol=6,
        live_max_volume_per_order=0.06,
        live_max_volume_per_symbol=0.06,
        live_max_daily_loss_limit_pct=25.0,
    )
    risk_cfg = SimpleNamespace(
        max_positions_per_symbol=6,
        max_volume_per_order=0.06,
        max_volume_per_symbol=0.06,
        daily_loss_limit_pct=25.0,
        max_trades_per_day=5,
        data_unavailable_policy="fail_closed",
        preflight_policy=policy,
    )

    monkeypatch.setattr(
        "src.config.topology.load_topology_group", lambda group_name: group
    )
    monkeypatch.setattr(
        live_preflight,
        "_load_instance_risk_config",
        lambda instance_name: risk_cfg,
        raising=False,
    )

    rows = live_preflight._check_instance_risk_safety("live")

    assert any(
        name == "[live-main] Risk: max_positions_per_symbol" and status == "OK"
        for name, status, _detail in rows
    )
    assert any(
        name == "[live-main] Risk: max_volume_per_order" and status == "OK"
        for name, status, _detail in rows
    )
    assert any(
        name == "[live-main] Risk: max_volume_per_symbol" and status == "OK"
        for name, status, _detail in rows
    )
    assert any(
        name == "[live-main] Risk: daily_loss_limit_pct" and status == "OK"
        for name, status, _detail in rows
    )


def test_instance_risk_safety_blocks_live_when_risk_data_policy_is_not_fail_closed(
    monkeypatch,
) -> None:
    group = SimpleNamespace(main="live-main", workers=(), instances=("live-main",))
    risk_cfg = SimpleNamespace(
        max_positions_per_symbol=1,
        max_volume_per_order=0.01,
        max_volume_per_symbol=0.01,
        daily_loss_limit_pct=2.0,
        max_trades_per_day=5,
        data_unavailable_policy="warn_only",
    )

    monkeypatch.setattr(
        "src.config.topology.load_topology_group", lambda group_name: group
    )
    monkeypatch.setattr(
        live_preflight,
        "_load_instance_risk_config",
        lambda instance_name: risk_cfg,
        raising=False,
    )

    rows = live_preflight._check_instance_risk_safety("live")

    assert any(
        name == "[live-main] Risk: data_unavailable_policy"
        and status == "FAIL"
        and "fail_closed" in detail
        for name, status, detail in rows
    )
