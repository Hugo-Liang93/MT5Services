# 配置指南

当前项目采用“共享主配置 + 模块专属配置”的结构。`config/app.ini` 负责共享运行范围，其余文件负责各模块专属行为。

## 配置文件总览

| 文件 | 用途 |
| --- | --- |
| `config/app.ini` | 共享交易范围、频率、限制、系统参数 |
| `config/market.ini` | API Host/Port、认证、日志 |
| `config/ingest.ini` | 采集器性能、重试、健康阈值 |
| `config/storage.ini` | 存储通道与批量写入策略 |
| `config/economic.ini` | 经济日历、Provider、交易风控 |
| `config/mt5.ini` | MT5 连接参数 |
| `config/db.ini` | 数据库连接参数 |
| `config/cache.ini` | 缓存兼容项 |
| `config/indicators.json` | 当前统一指标系统配置 |

## 继承关系

你可以把配置理解成三层：

1. `app.ini` 定义共享默认值
2. 模块专属 ini 在需要时覆盖本模块字段
3. 部分敏感项和运行项可由环境变量覆盖

## `.env` 与 `config/*.ini` 的职责划分

推荐遵循下面的边界，避免把密钥提交到仓库：

- `config/*.ini`：可审计、可共享、非敏感配置（默认值、功能开关、采集频率、限流参数）
- `.env`：敏感信息和临时覆盖（API Key、临时 Host/Port）
- `config/*.local.ini`：机器/环境私有覆盖（同样用于敏感信息），由 `.gitignore` 忽略

优先级（高 → 低）：

1. 环境变量（如 `MT5_API_KEY`、`MT5_API_HOST`、`MT5_API_PORT`）
2. `config/*.local.ini`（例如 `config/market.local.ini`）
3. 模块配置（例如 `config/market.ini`）
4. 共享配置（`config/app.ini`）

这样可以做到：

- 团队共享稳定默认值
- 本地/生产环境注入密钥不入库
- 出问题时仍可在 `monitoring/config/effective` 里看到来源

典型例子：

- API Host/Port：优先取 `market.ini[api]`，否则回退到 `app.ini[system]`
- 交易品种与时间框架：由 `app.ini[trading]` 统一提供
- 经济日历本地时区：默认继承系统时区
- 指标作用域：默认继承共享交易品种和时间框架

## 1. `config/app.ini`

这是共享配置中心。

### `trading`

- `symbols`
- `timeframes`
- `default_symbol`

### `intervals`

- `tick_interval`
- `ohlc_interval`
- `stream_interval`
- `indicator_reload_interval`

### `limits`

- `tick_limit`
- `ohlc_limit`
- `tick_cache_size`
- `ohlc_cache_limit`
- `quote_stale_seconds`

### `system`

- `timezone`
- `log_level`
- `modules_enabled`
- `api_host`
- `api_port`

示例：

```ini
[trading]
symbols = XAUUSD,EURUSD,USDJPY
timeframes = M1,M5,H1
default_symbol = XAUUSD

[intervals]
tick_interval = 0.5
ohlc_interval = 30.0
stream_interval = 1.0
indicator_reload_interval = 60
```

## 2. `config/market.ini`

管理 API 服务行为。

### `api`

- `host`
- `port`
- `enable_cors`
- `docs_enabled`
- `redoc_enabled`

### `security`

- `auth_enabled`
- `api_key_header`
- `api_key`

> 建议：`api_key` 不要写在 `config/market.ini`。优先使用环境变量 `MT5_API_KEY`，
> 或放到本地私有文件 `config/market.local.ini`。

### `logging`

- `access_log_enabled`
- `log_format`

示例：

```ini
[api]
host = 0.0.0.0
port = 8808
docs_enabled = true
redoc_enabled = true

[security]
auth_enabled = true
api_key_header = X-API-Key
api_key =
```

如果启用了 `auth_enabled`，但没有可用密钥，接口会直接拒绝请求。

## 3. `config/ingest.ini`

管理后台采集器。

典型职责：

- 初始回看时间
- OHLC 回补上限
- 重试次数与退避
- 最大并发符号数
- 队列监控频率
- 健康检查周期
- 可接受最大延迟

它直接影响：

- `BackgroundIngestor`
- 监控告警阈值
- 数据延迟判断

## 4. `config/storage.ini`

当前存储层是通道化设计。

### 全局 section

`[storage]` 常见字段：

- `queue_full_policy`
- `queue_put_timeout`
- `quote_flush_enabled`

### 通道 section

每个通道可配置：

- `type`
- `maxsize`
- `flush_interval`
- `batch_size`
- `page_size`
- `enabled`

当前代码里的主要通道包括：

- `ticks`
- `quotes`
- `intrabar`
- `ohlc`
- `ohlc_indicators`
- `economic_calendar`
- `economic_calendar_updates`

示例：

```ini
[ticks]
type = ticks
maxsize = 20000
flush_interval = 1.0
batch_size = 1000

[economic_calendar]
type = economic_calendar
maxsize = 1000
flush_interval = 2.0
batch_size = 100
```

## 5. `config/economic.ini`

负责经济日历抓取与交易前风控。

### `economic`

重点字段：

- `enabled`
- `lookback_days`
- `lookahead_days`
- `refresh_interval_seconds`
- `calendar_sync_interval_seconds`
- `near_term_refresh_interval_seconds`
- `release_watch_interval_seconds`
- `stale_after_seconds`
- `high_importance_threshold`
- `pre_event_buffer_minutes`
- `post_event_buffer_minutes`
- `curated_*`
- `trade_guard_*`

### `fred`

- `enabled`
- `api_key`

### `tradingeconomics`

- `enabled`
- `api_key`

重点理解：

- `trade_guard_enabled`：是否启用交易前风控
- `trade_guard_mode`：事件命中时是警告还是阻断
- `trade_guard_calendar_health_mode`：Provider 异常时如何处理
- `trade_guard_provider_failure_threshold`：Provider 失败阈值
- `pre_event_buffer_minutes` / `post_event_buffer_minutes`：风险窗口缓冲

示例：

```ini
[economic]
enabled = true
refresh_interval_seconds = 900
high_importance_threshold = 3
pre_event_buffer_minutes = 30
post_event_buffer_minutes = 30
trade_guard_enabled = true
trade_guard_mode = warn_only
trade_guard_lookahead_minutes = 180
```

## 6. `config/mt5.ini`

负责 MT5 终端连接：

- `login`
- `password`
- `server`
- `path`
- `timezone`

这部分错误通常会直接导致 `/health` 中市场连接失败。

## 7. `config/db.ini`

负责 PostgreSQL/TimescaleDB：

- `host`
- `port`
- `user`
- `password`
- `database`
- `schema`

## 8. `config/indicators.json`

这是当前统一指标系统的主配置。

顶层常见字段：

- `indicators`
- `pipeline`
- `inherit_symbols`
- `inherit_timeframes`
- `symbols`
- `timeframes`
- `auto_start`
- `hot_reload`
- `reload_interval`

单个指标字段：

- `name`
- `func_path`
- `params`
- `dependencies`
- `compute_mode`
- `enabled`
- `description`
- `tags`

示例：

```json
{
  "indicators": [
    {
      "name": "sma20",
      "func_path": "src.indicators.core.mean.sma",
      "params": {
        "period": 20,
        "min_bars": 20
      },
      "dependencies": [],
      "compute_mode": "standard",
      "enabled": true,
      "description": "20 周期简单移动平均",
      "tags": ["trend", "mean"]
    }
  ],
  "inherit_symbols": true,
  "inherit_timeframes": true,
  "auto_start": true,
  "hot_reload": true,
  "reload_interval": 60.0
}
```

`compute_mode` 可选：

- `standard`
- `incremental`
- `parallel`

## 环境变量覆盖

当前代码里明确支持的覆盖项主要有：

- `MT5_API_HOST`
- `MT5_API_PORT`
- `MT5_API_KEY`
- `TRADINGECONOMICS_API_KEY`
- `FRED_API_KEY`

推荐原则：

- 普通配置进 ini/json
- 密钥进环境变量
- 临时端口覆盖用环境变量

## 本地私有覆盖（避免提交密钥）

项目支持 `*.local.ini` 覆盖层，且已默认忽略 Git。

例如：

1. 复制模板：`cp config/market.local.ini.example config/market.local.ini`
2. 在 `config/market.local.ini` 中写入 `api_key`
3. 启动服务，运行时会自动覆盖 `config/market.ini` 同名字段

## 运行时验证

最直接的配置验证接口：

```bash
curl http://localhost:8808/monitoring/config/effective
```

这个接口适合排查：

- 共享配置是否已正确继承
- 指标作用域是否正确
- 热重载参数是否已生效
- API 运行时 Host/Port 和限流参数是否符合预期

## 常见修改场景

### 修改交易品种

改 `config/app.ini`：

```ini
[trading]
symbols = XAUUSD,EURUSD
default_symbol = XAUUSD
```

### 修改 API 端口

优先改 `config/market.ini`：

```ini
[api]
port = 8810
```

或者临时覆盖：

```bash
set MT5_API_PORT=8810
```

### 调整经济风险窗口

改 `config/economic.ini`：

```ini
[economic]
pre_event_buffer_minutes = 45
post_event_buffer_minutes = 45
trade_guard_lookahead_minutes = 240
```

### 调整写库吞吐

改 `config/storage.ini`：

```ini
[ticks]
maxsize = 50000
batch_size = 2000
flush_interval = 0.5
```

## 排查建议

常用检查顺序：

1. `GET /health`
2. `GET /monitoring/startup`
3. `GET /monitoring/config/effective`
4. `GET /monitoring/queues`
5. `GET /economic/calendar/status`
6. `GET /indicators/performance/stats`
