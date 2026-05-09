# Tick-Derived Trading Route Design

> Date: 2026-05-05  
> Status: design approved for planning, not implemented  
> Scope: full tick-derived trading route for MT5Services

## 1. Objective

Build a complete `tick-derived` trading route.

The route uses tick and quote data to calculate short-window features, then emits strategy signals from stable feature snapshots. It is not a tick-to-order scalping engine and it must not route every raw tick directly to trading execution.

Default first-stage cadence:

- Feature window: 5 seconds
- Rolling update: 1 second
- Execution style: normal execution intent route, not direct broker dispatch

## 2. Current Architecture Verdict

The current codebase can support this direction as a foundation, but it does not yet implement a complete tick-derived route.

Already available:

- MT5 tick fetch, in-memory tick cache, tick persistence, and API query.
- Strategy capability contract with `market_data_requirements`.
- Required market data lane health that can fail closed for `tick:<symbol>` and `quote:<symbol>`.
- Unified execution route through `execution_intents`, `ExecutionIntentConsumer`, and `TradeExecutor`.
- Trade trace and lifecycle projection that can be reused once tick feature metadata is attached.

Missing:

- Tick model does not preserve `bid`, `ask`, `last`, and `flags`.
- Tick writes do not emit a public tick event or tick batch listener.
- No `TickFeatureEngine` or `TickFeatureSnapshot` contract exists.
- `SignalRuntime` has only `confirmed` and `intrabar` queue semantics.
- Backtesting is OHLC / child-bar driven and cannot replay tick order.
- Ready, tradability, and pre-trade checks do not include tick feature health.

## 3. Non-Goals

- Do not build tick-to-order / every-tick direct execution.
- Do not disguise tick-derived as `intrabar`.
- Do not calculate tick features inside `BackgroundIngestor`.
- Do not make strategies poll `MarketDataService._tick_cache`.
- Do not validate tick-derived strategies with OHLC-only backtests.

## 4. Architecture

Target runtime flow:

```text
MT5 tick/quote
  -> BackgroundIngestor
  -> MarketDataService tick/quote cache
  -> MarketDataService tick batch event port
  -> TickFeatureEngine
       - rolling window by symbol
       - feature calculation
       - data quality classification
  -> TickFeatureSnapshotBus
  -> SignalRuntime(scope=tick_derived)
  -> ExecutionIntentPublisher
  -> ExecutionIntentConsumer
  -> TradeExecutor
```

## 5. Module Responsibilities

| Module | Owns | Inputs | Outputs | Must Not Do |
|---|---|---|---|---|
| `BackgroundIngestor` | MT5 fetch cadence and lane health | MT5 quote/tick/OHLC APIs | raw tick/quote writes into `MarketDataService` and storage | calculate features or evaluate strategies |
| `MarketDataService` | raw market data cache | tick/quote/OHLC updates | tick query APIs and tick batch event port | expose private cache as strategy input |
| `TickFeatureEngine` | rolling tick windows and feature health | tick batch events, quote read port | `TickFeatureSnapshot` stream and health snapshot | dispatch trades or own strategy state |
| `TickFeatureSnapshotBus` | bounded feature broadcast | feature snapshots | listeners for signal runtime and monitoring | persist business state |
| `SignalRuntime` | signal queues and strategy state | feature snapshots for `tick_derived` scope | `SignalEvent(scope=tick_derived)` | treat tick-derived as intrabar |
| `TradeExecutor` | pre-trade checks and execution dispatch | `SignalEvent` from intent consumer | broker dispatch result and execution audit | know tick feature internals |
| `TickReplayEngine` | replay-time event order and simulation | persisted ticks/quotes | replay metrics, trades, feature coverage | call MT5 or wall-clock time |

## 6. Data Contracts

### 6.1 Tick Model

`Tick` must preserve executable price information:

```text
symbol
bid
ask
last
price
volume
time
time_msc
flags
```

`price` can remain a normalized display price, but `bid/ask/last` must be stored independently. Replay and execution cost logic must use executable sides:

- buy entry uses ask
- sell entry uses bid
- buy exit uses bid
- sell exit uses ask

### 6.2 TickFeatureSnapshot

```text
symbol
window_seconds
emitted_at
market_time
quote_age_ms
tick_count
tick_rate_per_second
mid_price
spread_points
micro_return_points
realized_volatility_points
buy_pressure
sell_pressure
imbalance
burst_score
data_quality_status
source_tick_from
source_tick_to
```

`data_quality_status` values:

- `healthy`
- `warming_up`
- `stale`
- `sparse`
- `spread_wide`
- `quote_unavailable`

## 7. Signal Contract

Tick-derived strategies declare:

```python
preferred_scopes = ("tick_derived",)
market_data_requirements = ("tick", "quote")
```

`SignalRuntime` must expose `tick_derived` as a first-class scope:

- independent queue
- independent drop counters
- independent stale/drop rules
- independent status fields
- no reuse of intrabar coordinator semantics

All tick-derived `SignalEvent.metadata` must include:

```text
tick_feature_window_seconds
tick_feature_emitted_at
tick_feature_market_time
tick_feature_quote_age_ms
tick_feature_tick_count
tick_feature_spread_points
tick_feature_imbalance
tick_feature_burst_score
tick_feature_data_quality_status
```

## 8. Health And Admission

When any executable tick-derived strategy is scheduled for a symbol, readiness must require:

- `tick:<symbol>` required lane healthy.
- `quote:<symbol>` required lane healthy.
- `TickFeatureEngine.running=true`.
- `feature_snapshot_age_ms <= 2000`.
- `tick_count >= min_tick_count`.
- `data_quality_status` is not `stale`, `sparse`, `spread_wide`, or `quote_unavailable`.
- `tick_derived` queue depth is not persistently above threshold.
- execution consumers have progress health.

If tick feature health is critical:

- `/v1/monitoring/health/ready` returns not ready.
- `/v1/trade/state.tradability` reports `tick_feature_unhealthy`.
- `TradeAdmissionService` blocks new entry.
- `TradeExecutor` pre-trade filter blocks new entry.
- position management and close-only operations continue.

## 9. Backpressure

Tick-derived must be bounded by design:

- Raw tick ingestion cannot block on strategy evaluation.
- `TickFeatureEngine` keeps rolling windows per symbol, not unbounded queues.
- Snapshot bus is bounded and keeps the latest relevant snapshots.
- `SignalRuntime` has a dedicated `tick_derived` queue.
- Tick-derived queue pressure cannot starve `confirmed` bar-close events.
- Drop and replace counters must enter monitoring.

## 10. Replay And Backtest

Tick-derived requires a dedicated replay path:

```text
ticks / quotes from TimescaleDB
  -> TickReplayDataLoader
  -> TickFeatureEngine(replay mode)
  -> TickFeatureSnapshot sequence
  -> Tick-derived strategy evaluation
  -> TickReplayExecutionSimulator
  -> metrics / trace / coverage report
```

Live and replay must share:

- `TickFeatureCalculator`
- `TickFeatureSnapshot`
- tick-derived strategy `evaluate()`
- data quality rules

Replay must not:

- connect to MT5
- call real `TradeExecutor`
- use wall-clock time
- use OHLC order assumptions to replace tick order

First execution simulation model:

- entry uses the next executable tick/quote after the signal.
- buy uses ask, sell uses bid.
- SL/TP are checked against tick order.
- same-timestamp double-touch uses the worse result.
- sparse/stale/spread-wide windows skip entry.
- optional latency stress can replay with 500ms, 1000ms, and 2000ms delay.

Replay output must include:

- feature coverage by symbol and time window
- sparse/stale/spread-wide counts
- strategy evaluation counts
- hold/buy/sell counts
- execution approximation metrics
- latency-free baseline
- optional latency stress metrics

## 11. Phased Delivery

### Phase 1: Data Contract

- Extend `Tick` and `ticks` schema to include `bid`, `ask`, `last`, `flags`.
- Preserve backward-safe database migration behavior for existing rows by making new fields nullable where needed.
- Update validation, repositories, API schemas, and ingestion writes.

### Phase 2: Tick Event Port

- Add public tick batch listener support to `MarketEventBus` and `MarketDataService`.
- Make `extend_ticks()` dispatch tick batches after cache update.
- Add tests proving feature consumers do not read private cache state.

### Phase 3: Tick Feature Engine

- Add `src/market/tick_features/`.
- Implement pure `TickFeatureCalculator`.
- Implement `TickFeatureEngine` with rolling windows and health snapshot.
- Add bounded `TickFeatureSnapshotBus`.

### Phase 4: Runtime Integration

- Add `tick_derived` scope to capability validation, runtime queues, queue status, and filtering stats.
- Wire `TickFeatureSnapshotBus` into `SignalRuntime`.
- Add `tick_feature_health` to ready, readmodel tradability, admission, and executor pre-trade checks.

### Phase 5: Tick Replay

- Add tick/quote data loader.
- Add replay-mode feature engine.
- Add tick-derived signal replay adapter.
- Add conservative execution simulator using bid/ask order.
- Produce coverage and metrics reports.

### Phase 6: Demo Guarded Execution

- Enable tick-derived strategies only in demo validation status.
- Require replay report before demo auto trade.
- Run demo canary with trace and lifecycle verification.
- Promote to live guarded only after demo acceptance.

## 12. Acceptance Criteria

The route is acceptable only when all are true:

- A tick-derived strategy can run from `TickFeatureSnapshot` without reading raw cache internals.
- Tick-derived health can independently make ready not ready.
- Admission and executor both fail closed when tick features are stale/sparse/spread-wide.
- Replay and live use the same feature calculation.
- Replay can simulate bid/ask entry and exit from tick order.
- Trace can identify the exact tick feature window behind a signal.
- M1/M5 confirmed-bar route continues to run without being starved by tick-derived bursts.

## 13. Open Risks

- MT5 Python tick data quality may vary by broker and symbol.
- Existing historical ticks lack bid/ask fields; old data cannot support rigorous replay unless paired with quote history or excluded.
- Windows MT5 terminal IPC can still stall; existing MT5 circuit helps but does not provide hard real-time guarantees.
- Tick-derived can increase signal volume quickly; frequency reservation and cooldown defaults must remain conservative.

## 14. Current Review Findings To Fix

1. `src/clients/mt5_market.py`: tick model lacks bid/ask/last/flags.
2. `src/market/service.py`: tick cache writes have no public event outlet.
3. `src/signals/orchestration/runtime_processing.py`: scope queues collapse non-confirmed events into intrabar.
4. `src/backtesting/data/loader.py`: no tick replay loader.
5. `src/ingestion/ingestor.py`: feature SLO must not depend on sequential ingest loop timing.
6. `src/readmodels/runtime.py`: tradability lacks tick feature health.

## 15. Spec Self-Review

- No placeholder requirements remain.
- Scope is limited to tick-derived route, not generic high-frequency trading.
- Module ownership is explicit and avoids private cache polling.
- Replay is mandatory before demo trading.
- The design does not require compatibility branches or dual semantic paths.
