# CLAUDE.md — MT5Services Codebase Guide

This file is the primary reference for AI assistants working on MT5Services. It covers architecture, conventions, workflows, and rules to follow when making changes.

---

## Project Overview

**MT5Services** is a production-ready FastAPI trading platform that connects to MetaTrader 5 (MT5) terminals. It provides real-time market data, technical indicators, signal generation, risk management, and trade execution via REST API, backed by TimescaleDB (PostgreSQL with time-series hypertables).

- **Python**: 3.9–3.12
- **Framework**: FastAPI + uvicorn
- **Database**: TimescaleDB (PostgreSQL extension)
- **Trading terminal**: MetaTrader 5 (Windows-only Python binding)
- **Entry point**: `python app.py` or `uvicorn src.api:app`

---

## Repository Structure

```
MT5Services/
├── app.py                    # Main launcher (resolves host/port, starts uvicorn)
├── validate_config.py        # Config consistency validator (run before startup)
├── pyproject.toml            # Project metadata, dependencies, tool config
├── requirements.txt          # Runtime dependencies only
├── config/                   # All configuration files
│   ├── app.ini               # SINGLE SOURCE OF TRUTH: symbols, timeframes, intervals
│   ├── market.ini            # API server config (host, port, auth, CORS)
│   ├── mt5.ini               # MT5 terminal connection & account profiles
│   ├── db.ini                # TimescaleDB connection
│   ├── ingest.ini            # Background data ingestion settings
│   ├── storage.ini           # Multi-channel queue persistence settings
│   ├── economic.ini          # Economic calendar & trade guard windows
│   ├── risk.ini              # Risk limits (position counts, SL/TP requirements)
│   ├── cache.ini             # Cache compatibility settings
│   ├── signal.ini            # Signal module settings
│   └── indicators.json       # Indicator definitions & computation pipeline
├── src/
│   ├── api/                  # FastAPI routes, middleware, schemas, DI container
│   ├── clients/              # MT5 client wrappers (market, trading, account)
│   ├── config/               # Config loading, merging, Pydantic models
│   ├── core/                 # Core services (market cache, economic calendar, account)
│   ├── indicators/           # Unified indicator system (manager, engine, cache)
│   ├── ingestion/            # Background tick/OHLC/intrabar data collection
│   ├── persistence/          # TimescaleDB writer, queue-based storage writer
│   ├── risk/                 # Risk rules, models, service
│   ├── signals/              # Signal generation strategies, runtime, filters
│   ├── trading/              # TradingModule, account registry, signal executor
│   ├── monitoring/           # Health checks
│   └── utils/                # Shared utilities, event store, memory manager
├── tests/                    # Test suite mirroring src/ structure
└── docs/                     # Code review notes, internal docs
```

---

## Configuration System

### Critical Rule: `config/app.ini` is the Single Source of Truth

All trading symbols, timeframes, and global intervals are defined **only** in `config/app.ini`. Other `.ini` files reference or inherit from it. Never duplicate these settings elsewhere.

### Resolution Order (highest to lowest priority)

```
config/*.local.ini  (gitignored, for secrets & machine-specific overrides)
    ↓
config/*.ini        (checked-in base configuration)
    ↓
code defaults       (Pydantic model field defaults in src/config/centralized.py)
```

### Creating Local Overrides

Copy the example and edit:
```bash
cp config/market.local.ini.example config/market.local.ini
# Edit config/market.local.ini with your secrets/local values
```

### No `.env` Files

`.env` files are **deprecated**. All configuration lives in `.ini` files. `python-dotenv` remains as a dependency only for legacy compatibility.

### Key Config Files

| File | Purpose |
|------|---------|
| `config/app.ini` | Trading symbols, timeframes, intervals, cache limits, modules enabled |
| `config/market.ini` | API host/port, auth (API key), CORS |
| `config/mt5.ini` | MT5 terminal path, account profiles (login/password/server) |
| `config/db.ini` | PostgreSQL/TimescaleDB host, port, credentials |
| `config/ingest.ini` | Ingestion intervals, backfill limits, retry settings |
| `config/storage.ini` | Queue channel sizes, flush intervals, batch sizes |
| `config/economic.ini` | Calendar providers (FRED, TradingEconomics), risk windows |
| `config/risk.ini` | Max positions, SL/TP requirements |
| `config/indicators.json` | Indicator definitions, params, dependencies, pipeline config |

### Config Validation

Always run before startup when changing config:
```bash
python validate_config.py
```

---

## Architecture

### Startup Sequence

Components initialize in this order (see `src/api/deps.py` lifespan):

1. **Storage** — TimescaleDB writer + queue channels
2. **Ingestion** — Background market data collection thread
3. **Economic Calendar Service** — Fetches & caches calendar events
4. **Indicators** — Unified indicator manager
5. **Monitoring** — Health checks
6. **Trading Module** — Account registry, risk checks

Shutdown is reverse order. All components are singletons accessed via FastAPI's `Depends()`.

### Data Flow

```
MT5 Terminal
    ↓ (Background thread)
BackgroundIngestor (src/ingestion/ingestor.py)
    ↓                    ↓
MarketDataService    StorageWriter (queued channels)
(in-memory cache)        ↓
    ↓               TimescaleDB
API routes read
from cache
    ↓
OHLC close events → IndicatorManager → compute → persist
                  → SignalRuntime → strategies → execute trades
```

### In-Memory Cache Architecture

`MarketDataService` (`src/core/market_service.py`) is the **runtime source of truth**:
- Owns all tick, quote, OHLC, and intrabar state
- Thread-safe via `RLock`
- Single writer: `BackgroundIngestor`
- Multiple readers: API routes, indicator manager, signal runtime
- TimescaleDB is the **durable backup**, not the primary read path

### Multi-Channel Persistence

`StorageWriter` (`src/persistence/storage_writer.py`) uses separate async queues per data type:

| Channel | Queue Size | Flush Interval | Batch Size |
|---------|-----------|----------------|------------|
| ticks | 20,000 | 1s | 1,000 |
| quotes | 5,000 | 1s | 200 |
| intrabar | 10,000 | 5s | — |
| ohlc | 5,000 | 1s | — |
| ohlc_indicators | — | configurable | — |
| economic_calendar | — | configurable | — |

Queue-full policy is configurable: `auto` (block/drop), `block`, or `drop`.

### Event-Driven Indicators & Signals

- OHLC bar closes → `IndicatorManager` computes all indicators for that symbol/timeframe
- Intrabar events → `SignalRuntime` evaluates strategies
- Indicator computation supports: `standard`, `incremental`, and `parallel` modes (configured per indicator in `indicators.json`)

---

## Key Source Files

| File | Role |
|------|------|
| `src/api/deps.py` | DI container, startup/shutdown lifecycle, component singletons |
| `src/api/__init__.py` | FastAPI app, CORS middleware, API key auth, router registration |
| `src/config/centralized.py` | Pydantic config models (all config sections) |
| `src/config/advanced_manager.py` | Config loader: merges base + local .ini → Pydantic models |
| `src/core/market_service.py` | In-memory market data cache (thread-safe) |
| `src/ingestion/ingestor.py` | Background data collection (ticks, OHLC, intrabar) |
| `src/indicators/manager.py` | Unified indicator orchestrator (1200+ LOC) |
| `src/persistence/db.py` | TimescaleDB write operations, schema init, hypertables |
| `src/persistence/storage_writer.py` | Queue-based multi-channel persistence |
| `src/trading/service.py` | TradingModule: account, positions, orders, trade lifecycle |
| `src/signals/runtime.py` | Event-driven signal evaluation and trade execution |
| `src/core/economic_calendar_service.py` | Calendar sync, risk window computation, trade guard |

---

## API Endpoints

Base URL: `http://<host>:8808` (default)

Authentication: `X-API-Key` header (configured in `config/market.ini`)

| Router | Prefix | Key Endpoints |
|--------|--------|---------------|
| market | `/` | `/symbols`, `/quote`, `/ticks`, `/ohlc`, `/stream` (SSE) |
| trade | `/` | `/trade`, `/close`, `/close_all`, `/trade/precheck`, `/estimate_margin` |
| account | `/account` | `/account/info`, `/account/positions`, `/account/orders` |
| economic | `/economic` | `/economic/calendar`, `/economic/calendar/risk-windows`, `/economic/calendar/trade-guard` |
| indicators | `/indicators` | `/indicators/list`, `/indicators/{symbol}/{timeframe}`, `/indicators/compute` |
| monitoring | `/monitoring` | `/monitoring/health`, `/monitoring/startup`, `/monitoring/queues`, `/monitoring/config/effective` |
| signal | `/signal` | signal endpoints |

All responses use the generic wrapper:
```python
ApiResponse[T]:
    success: bool
    data: T | None
    error: str | None          # human-readable
    error_code: str | None     # AIErrorCode enum value
    metadata: dict | None
```

---

## Risk Management Layers

Trades pass through multiple checks (inner to outer):

1. **Signal filters** (`src/signals/filters.py`): economic event filter, spread filter, session filter
2. **Pre-trade risk service** (`src/core/pretrade_risk_service.py`): account-level checks
3. **Risk rules** (`src/risk/rules.py`): position limits, max volume, SL/TP requirements
4. **Trade guard** (`src/core/economic_calendar_service.py`): blocks trades in economic event windows

---

## Account Model

- **One MT5 account per service instance** (selected in `config/mt5.ini`)
- Multiple account profiles supported (`[account.demo]`, `[account.live]`) for easy instance switching
- `default_account` key in `[mt5]` section selects the active profile
- Account credentials live in `config/mt5.local.ini` (gitignored)

---

## Indicator System

Indicators are configured in `config/indicators.json`:

```json
{
  "name": "sma_20",
  "func_path": "src.indicators.core.mean.sma",
  "params": {"period": 20, "min_bars": 20},
  "dependencies": [],
  "compute_mode": "standard",
  "enabled": true,
  "tags": ["trend", "moving_average"]
}
```

Available compute modes:
- `standard` — full recompute on each bar close
- `incremental` — append-only update (faster for streaming)
- `parallel` — computed in parallel with other indicators

The pipeline (`src/indicators/engine/pipeline.py`) respects dependency order and uses a dependency graph for correct execution ordering.

---

## Development Workflows

### Installation

```bash
pip install -e ".[dev,test]"
```

### Running the Service

```bash
# Start service
python app.py

# Or with uvicorn directly
uvicorn src.api:app --host 0.0.0.0 --port 8808 --reload

# Validate config first
python validate_config.py
```

### Running Tests

```bash
# All tests
pytest

# Unit tests only
pytest -m unit

# Integration tests only
pytest -m integration

# Skip slow tests
pytest -m "not slow"

# With coverage
pytest --cov=src --cov-report=html

# Specific module
pytest tests/indicators/
```

### Code Quality

```bash
# Format code
black src/ tests/

# Sort imports
isort src/ tests/

# Type check
mypy src/

# Lint
flake8 src/ tests/
```

### Code Style (enforced by tools)

| Tool | Config | Line Length |
|------|--------|-------------|
| black | `[tool.black]` in pyproject.toml | 88 |
| isort | profile = "black" | 88 |
| mypy | strict mode (see pyproject.toml) | — |
| flake8 | `[tool.flake8]` | 88 |

---

## Code Conventions

### Type Safety

- **mypy strict mode** is enforced. All functions must have type annotations.
- Do not use `Any` without explicit justification.
- Pydantic models for all data boundaries (API request/response, config).

Mypy overrides (ignoring missing stubs for):
- `pandas.*`, `numpy.*`, `sqlalchemy.*`

### Pydantic Version

Uses **Pydantic v2** (`>=2.6.0`). Use v2 APIs:
- `model_validate()` not `parse_obj()`
- `model_dump()` not `dict()`
- `model_config = ConfigDict(...)` not `class Config:`

### Error Handling

- Use typed error codes (`AIErrorCode` enum in `src/api/error_codes.py`)
- Always return `ApiResponse[T]` wrapper from route handlers
- Include `suggested_action` in error responses where helpful
- Log with `structlog` (structured logging), not plain `logging`

### Thread Safety

- `MarketDataService` uses `RLock` — always acquire the lock for reads and writes
- `StorageWriter` queues are thread-safe — put items, don't access internals
- Do not share mutable state across threads without locks

### Configuration Access

Access config via `src/config/advanced_manager.py` public methods:
```python
from src.config import get_trading_config, get_api_config, get_db_config
# Not by direct .ini parsing
```

### Dependency Injection

Add new service components to `src/api/deps.py`:
- Register in the `AppDeps` container
- Add startup/shutdown in the lifespan context manager
- Expose via `FastAPI.Depends()` in route handlers

### Adding New Indicators

1. Implement the function in `src/indicators/core/` (following existing patterns in `mean.py`, `momentum.py`, etc.)
2. Add the entry to `config/indicators.json` with appropriate `func_path`, `params`, and `dependencies`
3. Add tests in `tests/indicators/`
4. Run `python validate_config.py` to verify

### Adding New API Routes

1. Create route file in `src/api/`
2. Add Pydantic request/response schemas to `src/api/schemas.py`
3. Register the router in `src/api/__init__.py`
4. Add appropriate error codes to `src/api/error_codes.py`

---

## Testing Conventions

### Test Structure

Tests mirror the `src/` structure:
```
tests/api/          → src/api/
tests/config/       → src/config/
tests/core/         → src/core/
tests/indicators/   → src/indicators/
tests/signals/      → src/signals/
tests/trading/      → src/trading/
tests/integration/  → end-to-end flows
tests/smoke/        → basic sanity checks
```

### Test Markers

Use markers to categorize:
```python
@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.slow
```

### Async Tests

Use `pytest-asyncio` for async test functions:
```python
import pytest

@pytest.mark.asyncio
async def test_something():
    ...
```

### Fixtures and Factories

- Use `factory-boy` for model factories
- Use `faker` for realistic test data
- Use `httpx.AsyncClient` for API integration tests

---

## Production Dependencies (Optional)

Install for production deployment:
```bash
pip install -e ".[prod]"
```

Adds: `gunicorn`, `uvloop`, `sentry-sdk`

---

## Known Issues & Notes

- **CORS**: Wildcard origin (`*`) cannot be combined with `allow_credentials=True` (fixed in `src/api/__init__.py`). If auth + CORS is needed, configure specific allowed origins.
- **MT5 Python binding**: Only available on Windows. Tests that require MT5 must be run on Windows or mocked.
- **TimescaleDB**: Must be set up with the TimescaleDB extension enabled before running the service. Schema is auto-initialized on first startup.
- **Config snapshot at import time**: `src/api/__init__.py` reads API config once at import. Changing `market.ini` requires a service restart.
- **Health endpoint**: `/health` couples liveness to all services. For Kubernetes deployments, consider separating liveness (`/health/live`) from readiness (`/health/ready`).

---

## File Naming Conventions

| Pattern | Convention |
|---------|-----------|
| Source modules | `snake_case.py` |
| Config files | `module_name.ini` |
| Local overrides | `module_name.local.ini` (gitignored) |
| Test files | `test_<module>.py` |
| Schema files | Named after the database table they represent |

---

## Git Workflow

- Default branch: `main`
- Development branches: `claude/<feature>-<id>` or descriptive feature branches
- Never push secrets — credentials go in `*.local.ini` (gitignored)
- Run `python validate_config.py` before committing config changes
- Run `black`, `isort`, `mypy`, `flake8` before committing code changes
