from __future__ import annotations

from src.config.runtime_identity import RuntimeIdentity, build_account_key
from src.trading.execution.executor import ExecutorConfig, TradeExecutor


class _TradingModule:
    pass


def test_trade_executor_reset_circuit_persists_account_scoped_history() -> None:
    rows: list[tuple] = []
    identity = RuntimeIdentity(
        instance_name="live-main",
        environment="live",
        instance_id="executor-main",
        instance_role="executor",
        live_topology_mode="multi_account",
        account_alias="main",
        account_label="Main",
        account_key=build_account_key("live", "Broker-Live", 1001),
        mt5_server="Broker-Live",
        mt5_login=1001,
        mt5_path="C:/MT5/main/terminal64.exe",
        peer_main_instance_id="live:live-main",
        run_id="run-test",
    )
    executor = TradeExecutor(
        trading_module=_TradingModule(),
        config=ExecutorConfig(),
        runtime_identity=identity,
        circuit_breaker_history_fn=lambda batch: rows.extend(batch),
    )
    executor.circuit_open = True
    executor.consecutive_failures = 3

    executor.reset_circuit()

    assert executor.circuit_open is False
    assert executor.consecutive_failures == 0
    assert len(rows) == 1
    assert rows[0][1] == "main"
    assert rows[0][2] == build_account_key("live", "Broker-Live", 1001)
    assert rows[0][3] == "executor"
    assert rows[0][4] == "reset"
    assert rows[0][5] == 3
