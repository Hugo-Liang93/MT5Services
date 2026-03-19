# 快速开始

这份文档只保留最短闭环：配置、启动、检查服务是否真的跑起来。

## 1. 安装

```bash
python -m venv .venv
```

```bash
# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```bash
pip install -U pip
pip install -e .
```

如果你还要开发或测试：

```bash
pip install -e ".[dev]"
pip install -e ".[test]"
```

## 2. 最少要检查的配置

首次启动至少改这 4 个文件：

- `config/mt5.ini`
- `config/db.ini`
- `config/app.ini`
- `config/market.ini`

如果你需要经济日历风控，再改：

- `config/economic.ini`

如果你需要指标计算，再看：

- `config/indicators.json`

## 3. 推荐最小配置

### `config/app.ini`

```ini
[trading]
symbols = XAUUSD,EURUSD
timeframes = M1,H1
default_symbol = XAUUSD

[intervals]
tick_interval = 0.5
ohlc_interval = 30.0
stream_interval = 1.0
indicator_reload_interval = 60

[limits]
tick_limit = 200
ohlc_limit = 200
tick_cache_size = 5000
ohlc_cache_limit = 500
quote_stale_seconds = 1.0

[system]
timezone = Asia/Shanghai
log_level = INFO
api_host = 0.0.0.0
api_port = 8808
```

### `config/mt5.ini`

```ini
[mt5]
login = 12345678
password = your_password
server = YourBroker-Server
path = C:/Program Files/MetaTrader 5/terminal64.exe
timezone = Asia/Shanghai
```

### `config/db.ini`

```ini
[db]
host = localhost
port = 5432
user = postgres
password = postgres
database = mt5
schema = public
```

### `config/market.ini`

```ini
[api]
host = 0.0.0.0
port = 8808
docs_enabled = true
redoc_enabled = true

[security]
auth_enabled = false
api_key_header = X-API-Key
api_key =
```

## 4. 启动

```bash
python app.py
```

## 5. 启动后先做 5 个检查

### 基础检查

```bash
curl http://localhost:8808/health
curl http://localhost:8808/monitoring/startup
curl http://localhost:8808/monitoring/config/effective
```

确认：

- `mode` 是 `unified`
- `market.connected` 为 `true`
- `startup.steps` 里的 `storage`、`ingestion`、`economic_calendar`、`indicators`、`monitoring` 都是 `ready`

### 市场数据检查

```bash
curl "http://localhost:8808/symbols"
curl "http://localhost:8808/quote?symbol=XAUUSD"
curl "http://localhost:8808/ohlc?symbol=XAUUSD&timeframe=M1&limit=20"
```

### 指标检查

```bash
curl "http://localhost:8808/indicators/list"
curl "http://localhost:8808/indicators/XAUUSD/M1"
```

### 经济日历检查

```bash
curl "http://localhost:8808/economic/calendar/status"
curl "http://localhost:8808/economic/calendar/upcoming?hours=24"
```

### 监控检查

```bash
curl "http://localhost:8808/monitoring/queues"
curl "http://localhost:8808/monitoring/economic-calendar"
curl "http://localhost:8808/monitoring/trading"
curl "http://localhost:8808/monitoring/components"
curl "http://localhost:8808/monitoring/trading/trigger-methods"
```

## 6. 常见问题

### `/health` 里 MT5 未连接

优先检查：

- MT5 终端是否已登录
- `config/mt5.ini` 的 `login`、`server`、`path`
- 当前运行用户是否能访问 MT5 终端

### 指标没有结果

优先检查：

- `config/indicators.json` 是否启用了指标
- 目标品种和时间框架是否在共享配置范围内
- 是否已经有足够的 bars 满足 `min_bars`

### 交易前检查拦截了下单

优先检查：

- `POST /trade/precheck`
- `GET /economic/calendar/trade-guard`
- `config/economic.ini` 中的 `trade_guard_*`

## 7. 下一步

- 需要完整配置说明：看 [CONFIG_GUIDE.md](CONFIG_GUIDE.md)
- 需要整体架构和目录说明：看 [README.md](README.md)
