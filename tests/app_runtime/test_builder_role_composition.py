from __future__ import annotations

from types import SimpleNamespace

from src.app_runtime.builder import build_app_container


def _signal_config():
    return SimpleNamespace(
        base_spread_points=0.0,
        max_spread_to_stop_ratio=0.5,
    )


def _risk_config():
    return SimpleNamespace(model_dump=lambda: {})


def _patch_builder_dependencies(
    monkeypatch,
    *,
    role: str,
    environment: str = "live",
    calls: list[tuple[str, dict]],
) -> None:
    monkeypatch.setattr(
        "src.app_runtime.builder.get_runtime_identity",
        lambda: SimpleNamespace(
            instance_role=role,
            instance_name=f"{role}-instance",
            environment=environment,
        ),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.get_runtime_ingest_settings",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.get_runtime_market_settings",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.get_economic_config",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.get_effective_config_snapshot",
        lambda: {},
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.get_risk_config",
        _risk_config,
    )

    def _record(name: str):
        def _inner(container, **kwargs):
            calls.append((name, kwargs))
            return None

        return _inner

    monkeypatch.setattr(
        "src.app_runtime.builder.build_market_layer",
        _record("market"),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.build_trading_layer",
        lambda container, **kwargs: calls.append(("trading", kwargs)) or None,
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.build_signal_layer",
        _record("signal"),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.build_paper_trading_layer",
        _record("paper"),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.build_account_runtime_layer",
        _record("account_runtime"),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.build_runtime_controls",
        _record("runtime_controls"),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.build_monitoring_layer",
        _record("monitoring"),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.build_runtime_read_models",
        _record("readmodels"),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder.build_studio_service_layer",
        _record("studio"),
    )


def test_build_app_container_executor_only_builds_account_runtime(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    _patch_builder_dependencies(monkeypatch, role="executor", calls=calls)
    signal_config = _signal_config()

    build_app_container(signal_config_loader=lambda: signal_config)

    call_names = [name for name, _ in calls]
    assert "market" in call_names
    assert "trading" in call_names
    assert "account_runtime" in call_names
    assert "signal" not in call_names
    assert "paper" not in call_names

    market_kwargs = next(kwargs for name, kwargs in calls if name == "market")
    assert market_kwargs["include_ingestion"] is False
    assert market_kwargs["include_indicators"] is False

    trading_kwargs = next(kwargs for name, kwargs in calls if name == "trading")
    assert trading_kwargs["enable_calendar_sync"] is False


def test_build_app_container_live_main_builds_paper_trading(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    _patch_builder_dependencies(
        monkeypatch,
        role="main",
        environment="live",
        calls=calls,
    )
    signal_config = _signal_config()

    build_app_container(signal_config_loader=lambda: signal_config)

    call_names = [name for name, _ in calls]
    assert "market" in call_names
    assert "trading" in call_names
    assert "signal" in call_names
    assert "paper" in call_names
    assert "account_runtime" not in call_names

    market_kwargs = next(kwargs for name, kwargs in calls if name == "market")
    assert market_kwargs["include_ingestion"] is True
    assert market_kwargs["include_indicators"] is True

    trading_kwargs = next(kwargs for name, kwargs in calls if name == "trading")
    assert trading_kwargs["enable_calendar_sync"] is True
