# Tick-Derived Trading Route Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete tick-derived trading route that converts MT5 tick and quote data into short-window feature snapshots, evaluates tick-derived strategies through a first-class runtime scope, gates execution on tick feature health, and validates strategies with tick replay before demo trading.

**Architecture:** Raw MT5 tick and quote data stays owned by ingestion and `MarketDataService`. A new tick batch event port publishes persisted tick facts without exposing private cache state. `TickFeatureEngine` consumes tick batches asynchronously, calculates bounded-window `TickFeatureSnapshot` objects, publishes feature snapshots, and owns feature freshness health. `SignalRuntime` treats `tick_derived` as a first-class scope with independent queues, backpressure, status, and strategy capability contracts. Trading admission and pre-trade checks consume `tick_feature_health` as hard facts for tick-derived strategies. Tick replay uses the same feature calculator and strategy contract with bid/ask-aware simulated execution.

**Tech Stack:** Python, FastAPI runtime, TimescaleDB/PostgreSQL, pytest, existing MT5 client abstractions, existing signal orchestration, execution intent, risk, and trade executor chain.

---

## Scope Gate

This change touches high-risk runtime paths: market ingestion, persistence, signal orchestration, trading admission, pre-trade checks, monitoring readiness, and backtesting. Implement the phases in order. Do not add production tick-derived strategies until replay, health gating, and demo canary validation are in place.

Module ownership for the final architecture:

- `src/clients/mt5_market.py`: MT5 raw tick model and broker API adaptation only.
- `src/persistence/`: durable tick storage schema and repository writes/reads only.
- `src/market/service.py` and `src/market/event_bus.py`: market data state and public event ports only.
- `src/market/tick_features/`: tick feature calculation, feature event publication, and feature health ownership.
- `src/signals/orchestration/`: scope-specific signal scheduling and strategy evaluation only.
- `src/trading/`: admission, risk, execution decisions, and refusal reasons only.
- `src/backtesting/tick_replay/`: replay-only data loading, clocking, and simulated execution.
- `src/api/monitoring_routes/health.py` and `src/readmodels/runtime.py`: public health and tradability projection only.

## File Structure

Modify these files:

- `src/clients/mt5_market.py`
- `src/persistence/schema/ticks.py`
- `src/persistence/schema/__init__.py`
- `src/persistence/repositories/market_repo.py`
- `src/persistence/validator.py`
- `src/market/service.py`
- `src/market/event_bus.py`
- `src/signals/contracts/capability.py`
- `src/signals/strategies/base.py`
- `src/signals/orchestration/runtime.py`
- `src/signals/orchestration/runtime_processing.py`
- `src/signals/orchestration/runtime_status.py`
- `src/readmodels/runtime.py`
- `src/trading/admission/service.py`
- `src/trading/execution/pre_trade_checks.py`
- `src/trading/execution/reasons.py`
- `src/app_runtime/builder_phases/signal.py`
- `src/app_runtime/runtime.py`
- `src/api/monitoring_routes/health.py`
- `docs/design/full-runtime-dataflow.md`
- `docs/runbooks/system-startup-and-live-canary.md`
- `docs/codebase-review.md`

Create these files:

- `src/market/tick_features/__init__.py`
- `src/market/tick_features/models.py`
- `src/market/tick_features/calculator.py`
- `src/market/tick_features/bus.py`
- `src/market/tick_features/engine.py`
- `src/market/tick_features/health.py`
- `src/backtesting/tick_replay/__init__.py`
- `src/backtesting/tick_replay/data_loader.py`
- `src/backtesting/tick_replay/execution.py`
- `src/backtesting/tick_replay/runner.py`
- `tests/clients/test_mt5_market_ticks.py`
- `tests/persistence/test_market_tick_schema.py`
- `tests/market/test_tick_event_bus.py`
- `tests/market/test_tick_feature_calculator.py`
- `tests/market/test_tick_feature_engine.py`
- `tests/signals/test_tick_derived_runtime.py`
- `tests/readmodels/test_tick_feature_health.py`
- `tests/trading/test_tick_feature_pretrade.py`
- `tests/backtesting/test_tick_replay.py`
- `tests/app_runtime/test_tick_derived_wiring.py`

---

## Task 1: Upgrade The Raw Tick Contract

- [ ] Add failing client tests in `tests/clients/test_mt5_market_ticks.py` that prove `Tick` keeps MT5 `bid`, `ask`, `last`, `volume`, `time`, `time_msc`, and `flags`.

  Use this concrete fixture shape:

  ```python
  from types import SimpleNamespace

  from src.clients.mt5_market import MT5MarketDataClient


  class FakeMT5:
      COPY_TICKS_ALL = 0

      def copy_ticks_from(self, symbol, from_time, count, flags):
          return [
              SimpleNamespace(
                  time=1_714_000_000,
                  time_msc=1_714_000_000_123,
                  bid=1.23450,
                  ask=1.23462,
                  last=1.23458,
                  volume=7,
                  flags=6,
              )
          ]


  def test_get_ticks_preserves_tradeable_tick_fields():
      client = MT5MarketDataClient(mt5_module=FakeMT5())

      ticks = client.get_ticks("EURUSD", from_time=None, count=1)

      assert len(ticks) == 1
      assert ticks[0].bid == 1.23450
      assert ticks[0].ask == 1.23462
      assert ticks[0].last == 1.23458
      assert ticks[0].volume == 7
      assert ticks[0].time == 1_714_000_000
      assert ticks[0].time_msc == 1_714_000_000_123
      assert ticks[0].flags == 6
  ```

- [ ] Run `python -m pytest tests\clients\test_mt5_market_ticks.py -q`.

  Expected result before implementation: the test fails because the current tick object does not expose all tradeable fields.

- [ ] Modify `src/clients/mt5_market.py` so the `Tick` dataclass contains this exact field set and keeps `price` as a computed property derived from `last`, then `mid`, then non-null side price.

  ```python
  @dataclass(frozen=True)
  class Tick:
      symbol: str
      time: int
      time_msc: int
      bid: float | None
      ask: float | None
      last: float | None
      volume: float
      flags: int

      @property
      def mid(self) -> float | None:
          if self.bid is None or self.ask is None:
              return None
          return (self.bid + self.ask) / 2.0

      @property
      def price(self) -> float | None:
          return self.last if self.last is not None else self.mid or self.bid or self.ask
  ```

- [ ] Update `MT5MarketDataClient.get_ticks()` to map MT5 rows into the new contract. Do not infer `bid` from `last` or `ask` from `bid`; missing source fields remain `None`.

- [ ] Add failing persistence tests in `tests/persistence/test_market_tick_schema.py` that assert `ticks` DDL includes `bid`, `ask`, `last`, `flags`, `price`, and that insert arguments include these columns in deterministic order.

- [ ] Modify `src/persistence/schema/ticks.py`:

  - Add nullable `bid DOUBLE PRECISION`, `ask DOUBLE PRECISION`, `last DOUBLE PRECISION`, and `flags INTEGER`.
  - Keep `price DOUBLE PRECISION` for read compatibility inside this codebase, populated from `Tick.price`.
  - Add `TICKS_MIGRATION_SQL` statements for these exact schema changes:

    ```sql
    ALTER TABLE ticks ADD COLUMN IF NOT EXISTS bid DOUBLE PRECISION;
    ALTER TABLE ticks ADD COLUMN IF NOT EXISTS ask DOUBLE PRECISION;
    ALTER TABLE ticks ADD COLUMN IF NOT EXISTS last DOUBLE PRECISION;
    ALTER TABLE ticks ADD COLUMN IF NOT EXISTS flags INTEGER;
    ```

  - Keep the hypertable declaration unchanged.

- [ ] Modify `src/persistence/schema/__init__.py` to import `TICKS_MIGRATION_SQL` and include it in `POST_INIT_DDL_STATEMENTS`.

- [ ] Modify `src/persistence/repositories/market_repo.py` so tick writes persist all tick fields and reject rows whose `price` property is `None`.

- [ ] Modify `src/persistence/validator.py` so tick validation checks:

  - `symbol` is non-empty.
  - `time` and `time_msc` are present.
  - at least one of `bid`, `ask`, `last`, or `price` is present.
  - `ask >= bid` when both sides are present.

- [ ] Run:

  ```powershell
  python -m pytest tests\clients\test_mt5_market_ticks.py tests\persistence\test_market_tick_schema.py -q
  ```

  Expected result after implementation: both test files pass.

---

## Task 2: Add A Public Tick Batch Event Port

- [ ] Add failing tests in `tests/market/test_tick_event_bus.py` for a new public event port:

  - `MarketDataService.add_tick_batch_listener(listener)` registers a listener.
  - `MarketDataService.remove_tick_batch_listener(listener)` unregisters it.
  - `MarketDataService.extend_ticks(symbol, ticks)` dispatches one immutable `TickBatchEvent`.
  - One listener exception is captured in event bus stats and does not block other listeners.

- [ ] Create `TickBatchEvent` in `src/market/event_bus.py`.

  ```python
  @dataclass(frozen=True)
  class TickBatchEvent:
      symbol: str
      ticks: tuple[Tick, ...]
      received_at: datetime
      latest_time_msc: int | None
  ```

- [ ] Extend `src/market/event_bus.py` with `TickBatchEventBus`:

  ```python
  class TickBatchEventBus:
      def __init__(self) -> None:
          self._listeners: list[Callable[[TickBatchEvent], None]] = []
          self._failed_dispatches = 0
          self._last_error: str | None = None

      def add_listener(self, listener: Callable[[TickBatchEvent], None]) -> None:
          if listener not in self._listeners:
              self._listeners.append(listener)

      def remove_listener(self, listener: Callable[[TickBatchEvent], None]) -> None:
          if listener in self._listeners:
              self._listeners.remove(listener)

      def publish(self, event: TickBatchEvent) -> None:
          for listener in tuple(self._listeners):
              try:
                  listener(event)
              except Exception as exc:
                  self._failed_dispatches += 1
                  self._last_error = str(exc)

      def stats(self) -> dict[str, object]:
          return {
              "listeners": len(self._listeners),
              "failed_dispatches": self._failed_dispatches,
              "last_error": self._last_error,
          }
  ```

- [ ] Modify `src/market/service.py` so `extend_ticks()` updates `_tick_cache` and then publishes a `TickBatchEvent` through the new bus. Do not expose `_tick_cache` to feature code.

- [ ] Add `tick_event_bus_stats()` to `MarketDataService` and include it in existing market service diagnostics.

- [ ] Run `python -m pytest tests\market\test_tick_event_bus.py -q`.

  Expected result after implementation: all tick event bus tests pass.

---

## Task 3: Build Pure Tick Feature Models And Calculator

- [ ] Create `src/market/tick_features/models.py` with these immutable data contracts:

  ```python
  @dataclass(frozen=True)
  class TickFeatureConfig:
      window_seconds: float = 5.0
      emit_interval_seconds: float = 1.0
      max_quote_age_ms: int = 1500
      max_spread_points: float = 30.0
      min_ticks_per_window: int = 3
      point_size: float = 0.00001


  @dataclass(frozen=True)
  class TickFeatureSnapshot:
      symbol: str
      window_start_msc: int
      window_end_msc: int
      generated_at: datetime
      tick_count: int
      bid: float | None
      ask: float | None
      last: float | None
      mid: float | None
      spread_points: float | None
      quote_age_ms: int | None
      realized_range_points: float | None
      price_change_points: float | None
      buy_pressure: float | None
      sell_pressure: float | None
      status: str
      reasons: tuple[str, ...]
  ```

- [ ] Add failing calculator tests in `tests/market/test_tick_feature_calculator.py`:

  - healthy snapshot calculates spread, range, price change, and quote age from ordered ticks.
  - empty window returns status `stale` and reason `no_ticks`.
  - fewer than `min_ticks_per_window` returns status `sparse`.
  - wide spread returns status `blocked` and reason `spread_wide`.
  - out-of-order input is sorted by `time_msc` before calculation.

- [ ] Create `src/market/tick_features/calculator.py` with a pure `TickFeatureCalculator.calculate(symbol, ticks, now)` method. It must not read MT5, database, runtime, or strategy state.

- [ ] Implement direction pressure without pretending MT5 flags are always reliable:

  - If `last` increases from previous tradeable price, count as buy pressure.
  - If `last` decreases, count as sell pressure.
  - If price is unchanged or unavailable, do not add pressure.
  - Normalize buy and sell pressure by directional move count.

- [ ] Run `python -m pytest tests\market\test_tick_feature_calculator.py -q`.

  Expected result after implementation: calculator tests pass without requiring database or MT5.

---

## Task 4: Add Tick Feature Event Bus, Engine, And Health Owner

- [ ] Create `src/market/tick_features/bus.py` with a bounded in-memory `TickFeatureBus`:

  - `publish(snapshot)` appends snapshots up to a configured maxlen.
  - `drain(max_items)` returns snapshots in FIFO order.
  - `stats()` returns queue depth, dropped count, and last publish time.

- [ ] Create `src/market/tick_features/health.py` with `TickFeatureHealth` and `TickFeatureHealthStore`.

  Required health fields:

  - `symbol`
  - `status`
  - `last_snapshot_at`
  - `last_window_end_msc`
  - `snapshot_age_seconds`
  - `queue_depth`
  - `dropped_snapshots`
  - `last_reasons`

- [ ] Add failing tests in `tests/market/test_tick_feature_engine.py`:

  - engine subscribed to tick batch events emits a feature snapshot only when `emit_interval_seconds` elapsed.
  - engine keeps per-symbol rolling windows bounded by `window_seconds`.
  - feature bus backlog increments queue depth.
  - health becomes `stale` when no snapshot has been emitted within `max_snapshot_age_seconds`.

- [ ] Create `src/market/tick_features/engine.py`:

  ```python
  class TickFeatureEngine:
      def __init__(
          self,
          calculator: TickFeatureCalculator,
          feature_bus: TickFeatureBus,
          health_store: TickFeatureHealthStore,
          config: TickFeatureConfig,
      ) -> None:
          self._calculator = calculator
          self._feature_bus = feature_bus
          self._health_store = health_store
          self._config = config
          self._buffers: dict[str, deque[Tick]] = {}
          self._last_emit_msc: dict[str, int] = {}

      def on_tick_batch(self, event: TickBatchEvent) -> None:
          if not event.ticks:
              return
          buffer = self._buffers.setdefault(event.symbol, deque())
          for tick in event.ticks:
              buffer.append(tick)
          latest_msc = max(tick.time_msc for tick in event.ticks)
          cutoff_msc = latest_msc - int(self._config.window_seconds * 1000)
          while buffer and buffer[0].time_msc < cutoff_msc:
              buffer.popleft()
          last_emit_msc = self._last_emit_msc.get(event.symbol)
          emit_delta_msc = int(self._config.emit_interval_seconds * 1000)
          if last_emit_msc is not None and latest_msc - last_emit_msc < emit_delta_msc:
              return
          snapshot = self._calculator.calculate(
              symbol=event.symbol,
              ticks=tuple(buffer),
              now=event.received_at,
          )
          self._feature_bus.publish(snapshot)
          self._health_store.update_from_snapshot(snapshot, self._feature_bus.stats())
          self._last_emit_msc[event.symbol] = latest_msc
  ```

- [ ] Ensure `TickFeatureEngine` never calls strategy evaluation or execution directly. Its only outputs are `TickFeatureSnapshot` objects and `TickFeatureHealthStore` updates.

- [ ] Run `python -m pytest tests\market\test_tick_feature_engine.py -q`.

  Expected result after implementation: engine tests pass.

---

## Task 5: Make `tick_derived` A First-Class SignalRuntime Scope

- [ ] Add `tick_derived` to the strategy capability contract in `src/signals/contracts/capability.py` and `src/signals/strategies/base.py`.

  The scope values after this task are exactly:

  - `confirmed`
  - `intrabar`
  - `tick_derived`

- [ ] Add failing tests in `tests/signals/test_tick_derived_runtime.py`:

  - `SignalRuntime.enqueue(scope="tick_derived", item=snapshot)` uses a dedicated tick queue.
  - tick queue overflow increments tick-specific dropped stats.
  - `scope="tick_derived"` is not routed into intrabar processing.
  - unknown scope raises a structured error instead of silently routing.

- [ ] Modify `src/signals/orchestration/runtime.py` to initialize a dedicated tick-derived queue, stats object, and worker path.

- [ ] Modify `src/signals/orchestration/runtime_processing.py` so enqueue routing is explicit:

  ```python
  if scope == "confirmed":
      queue = self._confirmed_queue
  elif scope == "intrabar":
      queue = self._intrabar_queue
  elif scope == "tick_derived":
      queue = self._tick_derived_queue
  else:
      raise SignalScopeError(f"unsupported signal runtime scope: {scope}")
  ```

- [ ] Modify `src/signals/orchestration/runtime_status.py` so runtime status includes:

  - `tick_derived_queue_depth`
  - `tick_derived_dropped`
  - `tick_derived_processed`
  - `tick_derived_failed`
  - `tick_derived_last_snapshot_at`

- [ ] Add a minimal test-only strategy class that declares `tick_derived` capability and records received snapshots. Use it only in tests.

- [ ] Run `python -m pytest tests\signals\test_tick_derived_runtime.py -q`.

  Expected result after implementation: tick-derived scope is isolated from confirmed and intrabar queues.

---

## Task 6: Wire Tick Feature Runtime Without Cross-Layer Private Access

- [ ] Add failing app runtime tests in `tests/app_runtime/test_tick_derived_wiring.py`:

  - runtime builder creates `TickFeatureEngine` when at least one enabled strategy declares `tick_derived`.
  - runtime builder does not create it when no enabled strategy declares `tick_derived`.
  - `MarketDataService` listener count increases by one when engine is installed.
  - shutdown unregisters the tick listener and stops engine-owned background resources.

- [ ] Modify `src/app_runtime/builder_phases/signal.py` so tick-derived runtime creation is capability-driven through the formal strategy registry/capability contract.

- [ ] Modify `src/app_runtime/runtime.py` to hold public references to:

  - `tick_feature_engine`
  - `tick_feature_bus`
  - `tick_feature_health_store`

  Do not let API/readmodel code inspect private runtime fields to discover these objects.

- [ ] Ensure tick feature wiring follows this data path:

  `MT5MarketDataClient -> Ingestor -> MarketDataService.extend_ticks -> TickBatchEventBus -> TickFeatureEngine -> TickFeatureBus -> SignalRuntime tick_derived queue`

- [ ] Add a runtime lifecycle test that starts and stops the builder-created engine twice and asserts no duplicate tick listeners remain.

- [ ] Run `python -m pytest tests\app_runtime\test_tick_derived_wiring.py -q`.

  Expected result after implementation: runtime wiring tests pass and listener lifecycle is deterministic.

---

## Task 7: Gate Tradability And Pre-Trade On Tick Feature Health

- [ ] Add failing readmodel tests in `tests/readmodels/test_tick_feature_health.py`:

  - `RuntimeReadModel` includes `tick_feature_health` per symbol.
  - stale tick feature health makes a tick-derived strategy non-tradable.
  - stale tick feature health does not block confirmed-bar strategies.

- [ ] Add failing trading tests in `tests/trading/test_tick_feature_pretrade.py`:

  - tick-derived execution intent is rejected when `tick_feature_health.status` is `stale`, `blocked`, or missing.
  - rejection reason is `TICK_FEATURE_HEALTH_UNAVAILABLE` or `TICK_FEATURE_HEALTH_BLOCKED`.
  - confirmed-bar execution intent does not require tick feature health.

- [ ] Modify `src/readmodels/runtime.py` so `market_data_health` and `tick_feature_health` are separate facts in tradability projection.

- [ ] Modify `src/trading/execution/reasons.py` to add:

  ```python
  TICK_FEATURE_HEALTH_UNAVAILABLE = "tick_feature_health_unavailable"
  TICK_FEATURE_HEALTH_BLOCKED = "tick_feature_health_blocked"
  ```

- [ ] Modify `src/trading/admission/service.py` to require tick feature health only for intents or strategies whose scope is `tick_derived`.

- [ ] Modify `src/trading/execution/pre_trade_checks.py` so pre-trade hard blocks when tick feature health is missing, stale, sparse beyond configured allowance, or blocked.

- [ ] Modify `src/api/monitoring_routes/health.py` so ready fails when tick-derived strategies are enabled and any enabled symbol has stale or blocked tick feature health.

- [ ] Run:

  ```powershell
  python -m pytest tests\readmodels\test_tick_feature_health.py tests\trading\test_tick_feature_pretrade.py -q
  ```

  Expected result after implementation: health projection and pre-trade blocking pass.

---

## Task 8: Add Bid/Ask-Aware Tick Replay

- [ ] Create `src/backtesting/tick_replay/data_loader.py` with `TickReplayDataLoader.load_ticks(symbol, start, end)` reading persisted tick fields in time order.

- [ ] Create `src/backtesting/tick_replay/execution.py` with a simulated execution model:

  - buy market fills at next available `ask`.
  - sell market fills at next available `bid`.
  - buy limit fills when a later `ask <= limit_price`.
  - sell limit fills when a later `bid >= limit_price`.
  - when stop-loss and take-profit are both touched in the same `time_msc`, fill the adverse side first.

- [ ] Create `src/backtesting/tick_replay/runner.py` that reuses `TickFeatureCalculator` and the tick-derived strategy capability contract.

- [ ] Add failing replay tests in `tests/backtesting/test_tick_replay.py`:

  - loader preserves tick ordering by `time_msc`.
  - replay emits the same feature snapshot values as the live calculator for the same tick sequence.
  - market execution uses next tick side price, not current mid price.
  - same-timestamp stop-loss and take-profit conflict resolves to the adverse result.
  - replay report includes tick coverage, feature coverage, average spread, max spread, and rejected signal count.

- [ ] Run `python -m pytest tests\backtesting\test_tick_replay.py -q`.

  Expected result after implementation: replay validates tick-derived execution costs and feature coverage without MT5.

---

## Task 9: Update Monitoring, Docs, And Review Ledger

- [ ] Modify `docs/design/full-runtime-dataflow.md` with the final tick-derived path:

  `MT5 ticks -> persisted ticks -> TickBatchEvent -> TickFeatureSnapshot -> SignalRuntime tick_derived -> execution intent -> admission -> risk -> executor`.

- [ ] Modify `docs/runbooks/system-startup-and-live-canary.md` so startup and demo canary require:

  - tick event bus listener count is correct.
  - tick feature queue depth is below configured threshold.
  - tick feature health for enabled symbols is fresh.
  - ready endpoint includes tick feature health when tick-derived strategies are enabled.
  - demo order validation for tick-derived strategies is allowed only after a replay report exists for the same strategy, symbol, and session profile.

- [ ] Modify `docs/codebase-review.md` to record:

  - raw tick contract upgrade completed.
  - tick event port added.
  - tick-derived runtime scope added.
  - tick feature health added to tradability and pre-trade.
  - tick replay added.
  - production strategy enablement remains blocked until a specific strategy passes replay and demo canary.

- [ ] Run documentation sanity checks:

  ```powershell
  Select-String -Path 'docs\design\full-runtime-dataflow.md','docs\runbooks\system-startup-and-live-canary.md','docs\codebase-review.md' -Pattern 'tick-derived|tick feature|TickFeature|tick_derived'
  ```

  Expected result: each edited document has concrete tick-derived references.

---

## Task 10: Final Verification

- [ ] Run all targeted tests from the implementation:

  ```powershell
  python -m pytest tests\clients\test_mt5_market_ticks.py tests\persistence\test_market_tick_schema.py tests\market\test_tick_event_bus.py tests\market\test_tick_feature_calculator.py tests\market\test_tick_feature_engine.py tests\signals\test_tick_derived_runtime.py tests\app_runtime\test_tick_derived_wiring.py tests\readmodels\test_tick_feature_health.py tests\trading\test_tick_feature_pretrade.py tests\backtesting\test_tick_replay.py -q
  ```

  Expected result: all targeted tests pass.

- [ ] Run compile verification:

  ```powershell
  python -m compileall src tests
  ```

  Expected result: compileall completes without syntax errors.

- [ ] Run health route smoke tests if the repository already has monitoring route tests:

  ```powershell
  python -m pytest tests\api -k "health or ready" -q
  ```

  Expected result: existing health tests pass or skip for unavailable external services.

- [ ] Inspect changed files:

  ```powershell
  git status --short
  git diff -- src\clients\mt5_market.py src\market src\signals\orchestration src\trading src\backtesting\tick_replay docs\design\full-runtime-dataflow.md docs\runbooks\system-startup-and-live-canary.md docs\codebase-review.md
  ```

  Expected result: changes are limited to tick-derived route contracts, wiring, health, replay, tests, and docs.

- [ ] Commit the completed implementation only after the targeted tests and compile verification pass:

  ```powershell
  git add src tests docs
  git commit -m "Add tick-derived trading route infrastructure"
  ```

  Expected result: commit succeeds with no unrelated generated artifacts.
