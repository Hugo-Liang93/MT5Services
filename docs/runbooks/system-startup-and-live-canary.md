# 系统启动巡检与 Live Canary Runbook

> 更新日期：2026-04-12
> 目标：把“服务能否正常启动、系统主链路是否真实打通、日志里哪些算真实问题”收口成一套可执行流程。
> 范围：只覆盖系统管线本身，不以真实交易下单是否成功作为本 runbook 的验收条件。

---

## 1. 权威观测面

这份 runbook 只依赖以下观测面，避免多处取数造成歧义：

| 观测面 | 位置 | 用途 |
|------|------|------|
| 主运行日志 | `data/logs/<instance>/mt5services.log` | 看启动顺序、线程存活、外部依赖异常 |
| 错误日志 | `data/logs/<instance>/errors.log` | 快速筛 WARNING / ERROR / EXCEPTION |
| 根健康探针 | `GET /health` | 看运行时组件快照和市场/交易摘要 |
| 就绪探针 | `GET /v1/monitoring/health/ready` | 看 `status=ready` 以及 `checks.storage_writer / ingestion / indicator_engine` 是否达到 ready |
| 队列视图 | `GET /v1/monitoring/queues` | 看 `writer_alive / ingest_alive` 与队列压力 |
| 指标性能视图 | `GET /v1/monitoring/performance` | 看 `event_loop_running` 等指标链路状态 |
| 事件存储视图 | `GET /v1/monitoring/events` | 看 indicator durable event queue 是否推进 |
| 预检 CLI | `python -m src.ops.cli.live_preflight --environment live` | 启动前检查 MT5 / DB / 配置一致性 |

---

## 2. 启动前检查

### 2.1 最小前置动作

1. 先运行只读预检：

```powershell
python -m src.ops.cli.live_preflight --environment live
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
4. 如果本次要打开自动交易，先确认两件事都已经显式配置：
   - 每个注册策略都有 `[strategy_deployment.<name>]`
   - 每个允许 live execution 的策略都在 `signal.ini` / `signal.local.ini` 里显式绑定到真实 `account_alias`

### 2.2 启动前必须明确的判断

| 判断项 | 要点 |
|------|------|
| 本次目标是不是“只验系统” | 如果是，只看采集/缓存/落库/事件消费/观测，不把“没有交易”判成失败 |
| 当前是否休盘 | 休盘时 `market_data data latency critical` 可能是预期现象 |
| 当前是否准备做开盘 canary | 如果是，至少预留 15 到 30 分钟观察窗口 |
| 当前是否打开自动交易 | 只有当 live 策略已显式 deployment + 显式 account binding 时才允许打开；否则应视为配置不完整 |

---

## 3. 标准启动步骤

### 3.1 启动服务

单实例：

```powershell
python -m src.entrypoint.web
```

命名实例（推荐多账户/多进程部署时使用）：

```powershell
python -m src.entrypoint.instance --instance live-main
```

按环境启动整组实例（推荐）：

```powershell
python -m src.entrypoint.supervisor --environment live
```

按拓扑组启动 `main + workers`：

```powershell
python -m src.entrypoint.supervisor --group live
```

### 3.2 并行观察日志

另开两个窗口：

```powershell
Get-Content -Path .\data\logs\<instance>\mt5services.log -Encoding utf8 -Wait
```

```powershell
Get-Content -Path .\data\logs\<instance>\errors.log -Encoding utf8 -Wait
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
| 主实例 ready | `/v1/monitoring/health/ready` | `main` 实例返回 `{"status":"ready"}`，且 `checks.storage_writer=ok`、`checks.ingestion=ok`、`checks.indicator_engine=ok` |
| 执行实例 ready | `/v1/monitoring/health/ready` | `executor` 实例返回 `{"status":"ready"}`，且 `checks.storage_writer=ok`、`checks.pending_entry=ok`、`checks.position_manager=ok`、`checks.account_risk_state=ok` |
| 采集线程 | `/health` 或 `/v1/monitoring/queues` | `ingest_alive=true` |
| 落库线程 | `/health` 或 `/v1/monitoring/queues` | `writer_alive=true` |
| 指标事件循环 | `/health` 或 `/v1/monitoring/performance` | `event_loop_running=true` |
| 信号运行时 | `/health` | `signal_runtime.running=true` |
| 入口日志 | `data/logs/<instance>/*.log` | 日志文件创建在实例隔离目录，而不是 `src/` 下或共享日志目录 |
| 运行期本地文件 | `data/runtime/<instance>/` | `events.db`、`signal_queue.db`、`health_monitor.db`、`health_alerts.db` 落在实例隔离目录；其中部分文件可能在组件首轮写入后创建 |
| 手工运行产物 | `data/artifacts/` | 回测 JSON、压测日志、启动排查输出统一放这里，不再使用根目录 `runtime/` |

### 4.2 失败条件

以下任一项出现，都按“真实系统问题”处理：

1. `/v1/monitoring/health/ready` 返回 503，或返回体中 `status != ready`。
2. `ingest_alive=false`、`writer_alive=false` 或 `event_loop_running=false`。
3. 日志持续出现 schema/check constraint 错误、启动 `TypeError`、线程崩溃、事件循环接口错误。
4. 运行期日志或 SQLite 文件重新落到 `src/` 下，或落回共享 `data/logs/`、`data/runtime/` 根目录。
5. 手工排障/压测/回测产物重新落到仓库根目录 `runtime/`。
6. `events.db` 视图长期不推进，同时 confirmed OHLC 已经在更新。

### 4.3 PowerShell 版快速判读顺序

1. 先看 `errors.log` 有没有连续异常。
2. 再看 `/v1/monitoring/health/ready` 是否稳定返回 `status=ready`，且 `checks.*=ok`。
3. 再看 `/health` 里的 `ingestor / storage_writer / indicator_engine / signal_runtime`。
4. 最后才看更细的 `queues / performance / events`。

多实例补充：

- `main` 和 `executor` 的 `/ready` 检查项不同，不能再用主实例口径去判定 worker。
- `executor` 的 `/health` 中 `ingestor.running=false`、`indicator_engine.running=false`、`signal_runtime.running=false` 是当前设计下的预期现象；如果其 `pending_entry / position_manager / account_risk_state` 正常，就不应判为启动失败。

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
| `economic calendar` 在启动早期显示 `warming_up=true`、`stale=false` | 启动引导期现象 | 表示服务尚未完成首次正式刷新窗口，不应按 stale 告警处理；只有超过 bootstrap deadline 仍无成功刷新才算真实 stale |
| `economic calendar` 重启后短时间显示 `health_state=refreshing`，但 `last_refresh_at` 已有值 | 正常恢复现象 | 表示上一轮成功刷新已从持久化状态恢复，本轮 `release_watch` 正在继续执行；这不是 stale，也不是恢复失败 |
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
5. 运行期日志和 SQLite 文件全部落在 `data/logs/<instance>/` 与 `data/runtime/<instance>/`。

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

1. `python -m src.ops.cli.live_preflight --environment live`
2. `python -m src.entrypoint.web`
3. 盯 `data/logs/errors.log`
4. 查 `/v1/monitoring/health/ready`
5. 查 `/health`
6. 查 `/v1/monitoring/queues`、`/v1/monitoring/performance`、`/v1/monitoring/events`
7. 如果在开盘窗口，再继续做 15 到 30 分钟 live canary

这套顺序的目标很明确：先判断“系统是否活着”，再判断“链路是否推进”，最后才讨论“策略和交易是否合理”。

## 8. 多实例部署补充说明

- 共享基线配置放在 `config/*.ini`。
- 实例覆盖配置放在 `config/instances/<instance>/*.ini` 与同目录 `.local.ini`。
- 实例目录只允许承载实例级配置：`mt5.ini`、`market.ini`、`risk.ini`。
- 当前环境由 `topology group` 唯一决定，`mt5.ini` 只描述账户连接参数。
- `app.ini`、`db.ini`、`signal.ini`、`topology.ini` 等共享配置不会从实例目录加载。
- `main` 实例只负责共享计算与统一观测；`worker` 只负责单账户执行与本地风控。
- 若主账户也交易，应让该实例同时具备共享计算面和本地 `AccountRuntime`，但它仍只对自己的账户负责。
- 若启用 `intrabar_trading.enabled=true`，必须保证 `signal.ini[intrabar_trading.trigger]` 中实际会用到的 parent TF，其 child TF 已包含在 `app.ini[trading].timeframes` 里；当前系统已对这条合同做 fail-fast 校验，不能再通过 `config/instances/<instance>/app.local.ini` 之类的实例级覆盖去补 `app.ini` 共享时间框架。
补充约束：

- 当前每条日志都会自动带 `environment / instance / role` 前缀，控制台与文件日志口径一致。
- `main` 与 `worker` 即使由 supervisor 同时拉起，也必须优先按 `data/logs/<instance>/` 分实例判读，不应再把混合控制台输出当成唯一事实源。
