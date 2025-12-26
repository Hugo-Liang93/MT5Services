# MT5 Market Data Service

Python FastAPI 服务，基于 MetaTrader5 终端实时获取行情、tick 与 K 线数据，并提供 REST/SSE 接口。

## 运行前准备
- 本机已安装并登录 MT5 终端（确保终端保持运行）。
- 安装依赖：`pip install -r requirements.txt`
- 配置环境变量（可放入 `.env`）：
  ```
  MT5_LOGIN=12345678
  MT5_PASSWORD=your_password
  MT5_SERVER=YourBroker-Server
  MT5_PATH="C:/Program Files/MetaTrader 5/terminal64.exe"  # 如需指定
  DEFAULT_SYMBOL=EURUSD
  TIMEZONE=UTC
  STREAM_INTERVAL_SECONDS=1.0
  TICK_LIMIT=200
  OHLC_LIMIT=200
  TICK_CACHE_SIZE=5000

  # 持续采集配置
  INGEST_SYMBOLS=EURUSD,XAUUSD
  INGEST_TICK_INTERVAL=0.5
  INGEST_OHLC_TIMEFRAMES=M1,H1
  INGEST_OHLC_INTERVAL=30
  # 批量落库控制
  TICK_FLUSH_INTERVAL=1.0
  TICK_FLUSH_BATCH_SIZE=500
  TICK_QUEUE_MAXSIZE=20000
  QUOTE_FLUSH_ENABLED=false
  QUOTE_FLUSH_INTERVAL=1.0
  QUOTE_FLUSH_BATCH_SIZE=200
  QUOTE_QUEUE_MAXSIZE=5000
  OHLC_FLUSH_INTERVAL=1.0
  OHLC_FLUSH_BATCH_SIZE=200
  OHLC_QUEUE_MAXSIZE=5000
  INTRABAR_ENABLED=true
  INTRABAR_FLUSH_INTERVAL=5.0
  INTRABAR_FLUSH_BATCH_SIZE=200
  INTRABAR_QUEUE_MAXSIZE=10000
  FLUSH_RETRY_ATTEMPTS=3
  FLUSH_RETRY_BACKOFF=1.0
  OHLC_UPSERT_OPEN_BAR=false
  OHLC_BACKFILL_LIMIT=500

  # TimescaleDB 连接
  PG_HOST=localhost
  PG_PORT=5432
  PG_USER=postgres
  PG_PASSWORD=postgres
  PG_DATABASE=mt5
  PG_SCHEMA=public
  ```

## 启动
```bash
python app.py
# 访问 http://localhost:8000/docs 查看接口文档
```

启动后会自动：
- 初始化 TimescaleDB schema（ticks/quotes/ohlc hypertable）。
- 背景线程按 `INGEST_SYMBOLS` 持续从 MT5 拉取 quote/tick/ohlc，更新内存缓存；tick/quote/ohlc 走缓冲+批量落库（quote 入库可通过 `QUOTE_FLUSH_ENABLED` 开关）。
- OHLC 默认只写已收盘 bar；如需盘中持续更新未收盘 bar，开启 `OHLC_UPSERT_OPEN_BAR=true`。
- 启动时按数据库最新时间补全 OHLC（最多 `OHLC_BACKFILL_LIMIT` 条），避免断档。

## 可用接口
- `GET /health` 检查连接状态
- `GET /symbols` 获取可用合约列表
- `GET /quote?symbol=EURUSD` 最新报价
- `GET /ticks?symbol=EURUSD&limit=200` 近期 ticks
- `GET /ohlc?symbol=EURUSD&timeframe=M1&limit=200` K 线数据
- `GET /stream?symbol=EURUSD` SSE 流式报价（默认 1s 轮询，可用 `interval` 调整）

## 结构
- `src/config/` 配置加载（.env 支持）
- `src/clients/mt5_market.py` MT5 行情封装（初始化、重连、行情/tick/K 线拉取）
- `src/core/market_service.py` 行情缓存与业务层
- `src/ingestion/ingestor.py` 后台采集线程（写缓存+TimescaleDB）
- `src/persistence/db.py` TimescaleDB schema/写入封装
- `src/api/__init__.py` FastAPI 路由与采集生命周期（market/account/trade 模块化）
- `app.py` 启动入口
- `src/indicators/` 指标模块（`ta.py` 基础指标及装饰器包装的标准化指标；`worker.py` 配置驱动后台计算，指标函数签名统一 `fn(bars, params) -> dict`，配置示例见 `config/indicators.ini`）
