# 系统启动巡检与 Live Canary Runbook

> 更新日期：2026-04-12
> 目标：把“服务能否正常启动、系统主链路是否真实打通、日志里哪些算真实问题”收口成一套可执行流程。
> 范围：只覆盖系统管线本身，不以真实交易下单是否成功作为本 runbook 的验收条件。

---

## 1. 权威观测面

这份 runbook 只依赖以下观测面，避免多处取数造成歧义：

| 观测面 | 位置 | 用途 |
|------|------|------|
| 主运行日志 | `data/logs/mt5services.log` | 看启动顺序、线程存活、外部依赖异常 |
| 错误日志 | `data/logs/errors.log` | 快速筛 WARNING / ERROR / EXCEPTION |
| 根健康探针 | `GET /health` | 看运行时组件快照和市场/交易摘要 |
| 就绪探针 | `GET /v1/monitoring/health/ready` | 看 `storage_writer / ingestion / indicator_engine` 是否达到 ready |
| 队列视图 | `GET /v1/monitoring/queues` | 看 `writer_alive / ingest_alive` 与队列压力 |
| 指标性能视图 | `GET /v1/monitoring/performance` | 看 `event_loop_running` 等指标链路状态 |
| 事件存储视图 | `GET /v1/monitoring/events` | 看 indicator durable event queue 是否推进 |
| 预检 CLI | `python -m src.ops.cli.live_preflight` | 启动前检查 MT5 / DB / 配置一致性 |

---

## 2. 启动前检查

### 2.1 最小前置动作

1. 先运行只读预检：

```powershell
python -m src.ops.cli.live_preflight
```

2. 确认运行期目录只落在项目根目录 `data/`，而不是 `src/`：

```powershell
Get-ChildItem -Path .\data
Get-ChildItem -Path .\src -Recurse -Directory | Where-Object { $_.FullName -match '\\data($|\\)|\\logs($|\\)' }
```

3. 确认本机 `config/*.local.ini` 覆盖是已知且可解释的，尤其是：
   - `signal.local.ini`
   - `risk.local.ini`
   - 任何会冻结策略、改变运行模式、关闭模块的本机覆盖

### 2.2 启动前必须明确的判断

| 判断项 | 要点 |
|------|------|
| 本次目标是不是“只验系统” | 如果是，只看采集/缓存/落库/事件消费/观测，不把“没有交易”判成失败 |
| 当前是否休盘 | 休盘时 `market_data data latency critical` 可能是预期现象 |
| 当前是否准备做开盘 canary | 如果是，至少预留 15 到 30 分钟观察窗口 |

---

## 3. 标准启动步骤

### 3.1 启动服务

```powershell
python -m src.entrypoint.web
```

### 3.2 并行观察日志

另开两个窗口：

```powershell
Get-Content -Path .\data\logs\mt5services.log -Encoding utf8 -Wait
```

```powershell
Get-Content -Path .\data\logs\errors.log -Encoding utf8 -Wait
```

### 3.3 并行观察探针

```powershell
curl.exe http://127.0.0.1:8808/health
curl.exe http://127.0.0.1:8808/v1/monitoring/health/ready
curl.exe http://127.0.0.1:8808/v1/monitoring/queues
curl.exe http://127.0.0.1:8808/v1/monitoring/performance
curl.exe http://127.0.0.1:8808/v1/monitoring/events
```

---

## 4. 启动后 5 分钟巡检

### 4.1 通过条件

| 模块 | 看哪里 | 通过条件 |
|------|------|------|
| Web 入口 | `/health` | 返回 200 |
| 主链路 ready | `/v1/monitoring/health/ready` | 返回 `ready`，且 `storage_writer=ok`、`ingestion=ok`、`indicator_engine=ok` |
| 采集线程 | `/health` 或 `/v1/monitoring/queues` | `ingest_alive=true` |
| 落库线程 | `/health` 或 `/v1/monitoring/queues` | `writer_alive=true` |
| 指标事件循环 | `/health` 或 `/v1/monitoring/performance` | `event_loop_running=true` |
| 信号运行时 | `/health` | `signal_runtime.running=true` |
| 入口日志 | `data/logs/*.log` | 日志文件创建在根目录 `data/logs/`，而不是 `src/` 下 |
| 运行期本地文件 | `data/` | `events.db`、`signal_queue.db`、`health_monitor.db`、`health_alerts.db` 落在根目录 `data/`；其中部分文件可能在组件首轮写入后创建 |

### 4.2 失败条件

以下任一项出现，都按“真实系统问题”处理：

1. `/v1/monitoring/health/ready` 返回 503。
2. `ingest_alive=false`、`writer_alive=false` 或 `event_loop_running=false`。
3. 日志持续出现 schema/check constraint 错误、启动 `TypeError`、线程崩溃、事件循环接口错误。
4. 运行期日志或 SQLite 文件重新落到 `src/` 下。
5. `events.db` 视图长期不推进，同时 confirmed OHLC 已经在更新。

### 4.3 PowerShell 版快速判读顺序

1. 先看 `errors.log` 有没有连续异常。
2. 再看 `/v1/monitoring/health/ready` 是否稳定为 `ready`。
3. 再看 `/health` 里的 `ingestor / storage_writer / indicator_engine / signal_runtime`。
4. 最后才看更细的 `queues / performance / events`。

---

## 5. 哪些日志算真实问题，哪些要结合时段判断

### 5.1 明确属于真实问题

| 现象 | 判定 | 原因 |
|------|------|------|
| `ready` 探针 503 | 阻塞问题 | 主链路关键组件未达到 ready |
| `writer_alive=false` / `ingest_alive=false` / `event_loop_running=false` | 阻塞问题 | 采集、落库或指标 durable 消费已断 |
| 重复出现 DB check constraint 失败 | 阻塞问题 | 持久化契约已漂移，系统会持续报错或进入 DLQ |
| 启动阶段出现 `TypeError` / 组件装配失败 | 阻塞问题 | 说明装配层与运行态契约未对齐 |
| `src/` 下再次出现运行期 `data`/`logs` | 配置回归 | 运行期文件锚点被写坏 |

### 5.2 不要直接误判成阻塞

| 现象 | 判定 | 如何处理 |
|------|------|------|
| 休盘窗口出现 `market_data data latency critical` | 时段相关现象 | 先确认是否休盘；休盘时这不等于链路断裂 |
| 经济日历外部源偶发 `read operation timed out` | 外部依赖退化 | 如果核心 ready/health 正常，可记为外部依赖风险，不单独判系统失败 |
| `/health` 中 `trade_executor.running=false` | 当前实现语义 | `TradeExecutor` 是懒启动 worker，未收到交易信号前不代表未装配 |
| 启动后短时间没有信号或没有交易 | 不足以判失败 | 本 runbook 验的是系统链路，不是策略一定出信号 |

---

## 6. 开盘窗口 Live Canary

### 6.1 目标

这一步不是验证“能不能交易赚钱”，而是验证开盘后系统主链路是否在真实 feed 下持续稳定：

```text
MT5 行情 -> BackgroundIngestor -> MarketDataService
-> StorageWriter -> TimescaleDB / runtime files
-> closed-bar event -> events.db
-> IndicatorEventLoop -> signal runtime
```

### 6.2 观察窗口

- 最短 15 分钟
- 更稳妥是 30 分钟
- 只在目标市场开盘且有真实行情流入时执行

### 6.3 Canary 步骤

1. 开盘前 5 分钟先完成第 2 节预检和第 4 节启动后巡检。
2. 开盘后第 0 到 2 分钟：
   - 看 `/health` 是否仍返回 200；
   - 看 `errors.log` 是否出现连续异常；
   - 确认没有新的装配失败或线程退出。
3. 开盘后第 2 到 5 分钟：
   - 看 `/v1/monitoring/health/ready` 是否持续为 `ready`；
   - 看 `writer_alive / ingest_alive / event_loop_running` 是否保持为真。
4. 开盘后第 5 到 15 分钟：
   - 看 `/v1/monitoring/events` 是否在推进；
   - 看 `events.db` 对应事件视图是否不再停滞；
   - 看 `errors.log` 是否出现持续刷屏的持久化异常。
5. 开盘后第 15 到 30 分钟：
   - 看主日志中是否出现重复队列阻塞、线程重启、事件循环中断；
   - 看系统仍然只向根目录 `data/` 写运行期文件。

### 6.4 Canary 通过标准

满足以下条件即可判“系统通路通过”：

1. `ready` 探针在整个观察窗口内稳定可用。
2. `ingest_alive / writer_alive / event_loop_running` 全程未掉。
3. 没有重复的 schema/contract/type error。
4. `events.db` 与监控事件视图能体现事件持续推进。
5. 运行期日志和 SQLite 文件全部落在根目录 `data/`。

### 6.5 Canary 失败后如何归类

| 失败现象 | 先归到哪个层次 |
|------|------|
| 没有新鲜行情，但线程都活着 | 环境 / 时段 / MT5 feed 问题 |
| 采集活着，但 `events.db` 不推进 | 指标 durable event 链路问题 |
| `writer_alive=false` 或队列异常堆积 | 持久化链路问题 |
| `event_loop_running=false` | indicators runtime 问题 |
| 只有经济日历外部源超时 | 外部依赖问题 |

---

## 7. 建议的固定执行顺序

每次准备验证系统时，固定走下面的序列，不要跳步：

1. `python -m src.ops.cli.live_preflight`
2. `python -m src.entrypoint.web`
3. 盯 `data/logs/errors.log`
4. 查 `/v1/monitoring/health/ready`
5. 查 `/health`
6. 查 `/v1/monitoring/queues`、`/v1/monitoring/performance`、`/v1/monitoring/events`
7. 如果在开盘窗口，再继续做 15 到 30 分钟 live canary

这套顺序的目标很明确：先判断“系统是否活着”，再判断“链路是否推进”，最后才讨论“策略和交易是否合理”。
