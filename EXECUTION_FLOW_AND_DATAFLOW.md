# MT5Services 执行顺序与数据流（统一入口）

## 1. 唯一入口

- 启动命令: `python app.py`
- Uvicorn 目标: `src.api:app`
- 不再区分 simple/enhanced 入口脚本

## 2. 启动顺序

1. `app.py` 读取 `config/app.ini`
2. 解析 `api_host` 与 `api_port`（环境变量可覆盖）
3. 启动 `uvicorn src.api:app`
4. `src/api/__init__.py` 创建并装配统一 FastAPI app
5. `src/api/deps.py` 在 lifespan 中启动后台组件

## 3. 统一功能集合

`src/api/deps.py` 统一初始化并注入以下组件：

1. `MarketDataService`
2. `StorageWriter` + `TimescaleWriter`
3. `BackgroundIngestor`
4. `EnhancedIndicatorWorker`
5. `HealthMonitor` + `MonitoringManager`
6. `AccountService`
7. `TradingService`

## 4. 路由装配

`src/api/__init__.py` 固定注册：

1. `market` 路由
2. `account` 路由
3. `trade` 路由
4. `monitoring` 路由
5. `/health` 健康检查

## 5. 核心数据流

### 5.1 行情链路

1. `BackgroundIngestor` 拉取 quote/ticks/ohlc
2. 写入 `MarketDataService` 缓存
3. API 市场接口直接读缓存返回

### 5.2 落库链路

1. Ingestor 调用 `storage.enqueue(channel, row)`
2. `StorageWriter` 后台批量 flush
3. `TimescaleWriter.write_*` 写入数据库

### 5.3 指标链路

1. `EnhancedIndicatorWorker` 消费事件并计算指标
2. 回写缓存/快照并持久化
3. 失败事件走重试机制

### 5.4 监控链路

1. `MonitoringManager` 周期检查队列、延迟、指标新鲜度、性能
2. `HealthMonitor` 记录指标与告警
3. `/monitoring/*` 提供监控查询与运维操作

