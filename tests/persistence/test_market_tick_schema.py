from __future__ import annotations

from datetime import datetime, timezone

from src.persistence import schema
from src.persistence.repositories.market_repo import MarketRepository
from src.persistence.schema import ticks
from src.persistence.validator import DataValidator


class _Writer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[tuple], int]] = []

    def _batch(self, sql: str, rows: list[tuple], page_size: int = 1000) -> None:
        self.calls.append((sql, rows, page_size))


def test_ticks_schema_contains_tradeable_tick_fields() -> None:
    ddl = ticks.DDL
    assert "bid         double precision" in ddl
    assert "ask         double precision" in ddl
    assert "last        double precision" in ddl
    assert "flags       integer" in ddl
    assert "price       double precision" in ddl
    assert (
        "ALTER TABLE ticks ADD COLUMN IF NOT EXISTS bid DOUBLE PRECISION"
        in ticks.MIGRATION_SQL
    )
    assert "SET last = price" in ticks.MIGRATION_SQL
    assert schema.TICKS_MIGRATION_SQL in schema.POST_INIT_DDL_STATEMENTS


def test_insert_ticks_sql_uses_full_tick_contract_order() -> None:
    assert ticks.INSERT_SQL.startswith(
        "INSERT INTO ticks (symbol, price, bid, ask, last, volume, time, time_msc, flags)"
    )


def test_market_repository_writes_full_tick_contract() -> None:
    writer = _Writer()
    repo = MarketRepository(writer)  # type: ignore[arg-type]
    recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()

    repo.write_ticks(
        [
            (
                "EURUSD",
                1.23458,
                1.23450,
                1.23462,
                1.23458,
                7.0,
                recorded_at,
                1_767_225_600_000,
                6,
            )
        ]
    )

    assert writer.calls == [
        (
            ticks.INSERT_SQL,
            [
                (
                    "EURUSD",
                    1.23458,
                    1.23450,
                    1.23462,
                    1.23458,
                    7.0,
                    recorded_at,
                    1_767_225_600_000,
                    6,
                )
            ],
            1000,
        )
    ]


def test_tick_validator_rejects_missing_price_source_and_crossed_quotes() -> None:
    recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()

    valid, msg = DataValidator.validate_tick(
        "EURUSD",
        None,
        None,
        None,
        None,
        1.0,
        recorded_at,
        1_767_225_600_000,
        0,
    )
    assert not valid
    assert "price source" in msg

    valid, msg = DataValidator.validate_tick(
        "EURUSD",
        1.2345,
        1.2350,
        1.2340,
        1.2345,
        1.0,
        recorded_at,
        1_767_225_600_000,
        0,
    )
    assert not valid
    assert "Bid > Ask" in msg


def test_tick_validator_requires_time_msc() -> None:
    recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()

    valid, msg = DataValidator.validate_tick(
        "EURUSD",
        1.2345,
        1.2344,
        1.2346,
        1.2345,
        1.0,
        recorded_at,
        None,
        0,
    )

    assert not valid
    assert "time_msc" in msg
