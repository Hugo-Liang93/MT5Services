# Configuration Guide

Runtime configuration is `ini/json` only.

- `config/*.ini`: shared configuration
- `config/*.local.ini`: local or production overrides
- `config/indicators.json`: indicator configuration

`.env` is not a runtime configuration source.

## Priority

Runtime resolution order:

1. `config/*.local.ini`
2. matching base file in `config/*.ini`
3. code defaults

## MT5 Runtime Model

`config/mt5.ini` may define multiple account profiles, but one service instance uses exactly one MT5 account for its full lifetime.

- `default_account` selects the active startup account
- runtime API does not support account switching
- trade/account monitoring and audit records are tagged with the active `account_alias`
- if you need multiple accounts online at once, run multiple service instances

This is intentional because the MT5 Python binding uses a global terminal session. In-process account switching causes data gaps and unstable ingestion.

## Key Files

- `config/app.ini`: shared trading scope and system defaults
- `config/market.ini`: API host, auth, logging
- `config/ingest.ini`: ingestion, backfill, retry, health thresholds
- `config/storage.ini`: persistence queues and flush strategy
- `config/economic.ini`: calendar and pre-trade economic risk settings
- `config/risk.ini`: risk limits and final execution session guard
- `config/mt5.ini`: MT5 startup account selection and terminal connection
- `config/db.ini`: PostgreSQL/TimescaleDB connection
- `config/cache.ini`: runtime cache overrides for in-memory limits
- `config/signal.ini`: signal runtime, voting, circuit breaker, session spread limits, market structure
- `config/indicators.json`: indicator pipeline configuration

## MT5 Example

```ini
[mt5]
default_account = live
path = C:/Program Files/MetaTrader 5/terminal64.exe
timezone = UTC

[account.live]
enabled = true
label = Live
login = 123456
password =
server = Broker-Live

[account.demo]
enabled = false
label = Demo
login = 234567
password =
server = Broker-Demo
```

## Local Overrides

Put secrets and machine-specific values in:

- `config/mt5.local.ini`
- `config/db.local.ini`
- `config/market.local.ini`
- `config/economic.local.ini`
- `config/risk.local.ini`

## XAUUSD Intraday Tuning

The runtime now supports a more opinionated XAUUSD intraday profile:

- `config/app.ini`
  - shared timeframes now default to `M1,M5,M15,H1`
- `config/signal.ini`
  - `session_spread_limits`: dynamic spread caps by session
  - `strategy_sessions`: route breakout/trend strategies to London/New York and mean-reversion to Asia/London
  - `strategy_timeframes`: constrain fast strategies to `M1/M5` and breakout/confirmation strategies to `M5/M15/H1`
  - `circuit_breaker`: auto-trade execution breaker
  - `contract_sizes`: per-symbol sizing inputs
  - `market_structure`: previous-day high/low, Asia range, London open range, compression/expansion context
  - `execution_costs`: block auto-trade when spread consumes too much of the planned stop distance
- `config/indicators.json`
  - added fast intraday indicators: `rsi5`, `macd_fast`, `ema21`, `ema55`
  - disabled demo-volume indicators: `mfi14`, `obv30`
  - `delta_bars` now emits short-horizon change-rate fields such as `rsi_d3`, `rsi_d5`, `macd_d3`
- signal defaults
  - regime thresholds tuned to `ADX 23/18` with `BB 0.8%`
  - calibrator defaults tuned to `alpha=0.15`, `min_samples=50`, `recency_hours=8`
  - `soft_regime_enabled = true` enables probability-weighted regime affinity
  - staged calibration is active: `<50 -> 0.0`, `50-99 -> 0.10`, `100+ -> 0.15`
  - HTF auto-trade filter is soft: conflict `0.70x`, alignment `1.10x`
- `config/risk.ini`
  - `allowed_sessions`: final trade execution session guard
  - `daily_loss_limit_pct`: stop new trades after the configured daily loss threshold
  - `require_sl_for_market_orders = true`: market orders now require an SL by default for XAUUSD intraday safety
- `config/economic.ini`
  - `trade_guard_mode = block`: high-impact economic windows now block XAUUSD entries by default instead of warn-only

## Runtime Safety

- `config/signal.ini`
  - `max_concurrent_positions_per_symbol`: signal-driven concurrent position cap per symbol
  - `session_transition_cooldown_minutes`: session handoff cooldown around `13:00 UTC`
  - `end_of_day_close_enabled`, `end_of_day_close_hour_utc`, `end_of_day_close_minute_utc`: UTC end-of-day closeout handled by `PositionManager`
  - `market_structure_m1_lookback_bars`: shorter lookback for M1 market-structure analysis
  - startup now runs a `PositionManager.sync_open_positions()` reconcile so existing XAUUSD positions are rehydrated after restart
- `config/risk.ini`
  - `daily_loss_limit_pct`: final pre-trade risk stop, surfaced through trade APIs as `daily_loss_limit`
- sizing
  - `src/trading/sizing.py` applies timeframe-specific ATR stop/target defaults for `M1`, `M5`, `M15`, and `H1`
- trade API
  - `GET /trade/control`: inspect manual trade-control state and executor circuit status
  - `POST /trade/control`: pause auto-entry, switch to close-only mode, and optionally reset the execution circuit
  - `POST /trade/reconcile`: manually resync tracked open positions with MT5 state
- monitoring
  - `GET /signals/monitoring/quality/{symbol}/{timeframe}` exposes regime diagnostics and confirmed-signal quality metrics for one symbol/timeframe pair

## Verification

```bash
curl http://localhost:8808/monitoring/config/effective
curl http://localhost:8808/monitoring/health
curl http://localhost:8808/trade/accounts
curl http://localhost:8808/signals/monitoring/quality/XAUUSD/M5
```

`/trade/accounts` returns the current active account profile for this instance, not all configured accounts.
