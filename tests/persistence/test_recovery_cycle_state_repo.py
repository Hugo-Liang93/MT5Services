from datetime import datetime, timezone

from src.persistence.repositories.trading_state_repo import TradingStateRepository


class DummyWriter:
    def __init__(self):
        self.batch_calls = []

    def _json(self, value):
        return {"json": value}

    def _batch(self, sql, rows, page_size=None):
        self.batch_calls.append((sql, rows, page_size))


def test_repository_writes_recovery_cycle_states_with_json_metadata():
    writer = DummyWriter()
    repo = TradingStateRepository(writer)
    now = datetime(2026, 5, 6, 1, 2, 3, tzinfo=timezone.utc)
    row = (
        "demo-main",
        "demo:broker:1001",
        "cycle-1",
        "XAUUSD",
        "buy",
        "tick_martingale_probe",
        "TICK",
        "sig-1",
        "open",
        "initial_opened",
        0.01,
        0.01,
        1,
        2300.0,
        2300.0,
        now,
        now,
        None,
        None,
        None,
        {"probe": True},
        now,
    )

    repo.write_recovery_cycle_states([row])

    assert len(writer.batch_calls) == 1
    sql, rows, page_size = writer.batch_calls[0]
    assert "INSERT INTO recovery_cycle_states" in sql
    assert rows[0][20] == {"json": {"probe": True}}
    assert page_size == 200


def test_repository_fetches_recovery_cycle_states_with_account_and_status_filters():
    writer = DummyWriter()
    repo = TradingStateRepository(writer)
    captured = {}

    def fake_fetch(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{"cycle_id": "cycle-1"}]

    repo._fetch_dicts = fake_fetch

    rows = repo.fetch_recovery_cycle_states(
        account_key="demo:broker:1001",
        statuses=["open", "blocked"],
        symbol="XAUUSD",
        strategy="tick_martingale_probe",
        cycle_id="cycle-1",
        source_signal_id="sig-1",
        limit=25,
    )

    assert rows == [{"cycle_id": "cycle-1"}]
    assert "account_key = %s" in captured["sql"]
    assert "status = ANY(%s)" in captured["sql"]
    assert "symbol = %s" in captured["sql"]
    assert "strategy = %s" in captured["sql"]
    assert "cycle_id = %s" in captured["sql"]
    assert "source_signal_id = %s" in captured["sql"]
    assert "LIMIT %s" in captured["sql"]
    assert captured["params"] == [
        "demo:broker:1001",
        ["open", "blocked"],
        "XAUUSD",
        "tick_martingale_probe",
        "cycle-1",
        "sig-1",
        25,
    ]
