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
- `config/risk.ini`: risk limits
- `config/mt5.ini`: MT5 startup account selection and terminal connection
- `config/db.ini`: PostgreSQL/TimescaleDB connection
- `config/cache.ini`: cache compatibility settings
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

## Verification

```bash
curl http://localhost:8808/monitoring/config/effective
curl http://localhost:8808/monitoring/health
curl http://localhost:8808/trade/accounts
```

`/trade/accounts` returns the current active account profile for this instance, not all configured accounts.
