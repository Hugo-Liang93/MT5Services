# 运行入口映射

> 更新日期：2026-04-26（追加 §0aa/§0bb 启动链生命周期约束；2026-04-12 初版）
> 本文只回答“从哪里启动”和“启动后先看哪里”；全量运行时链路请对照 `docs/design/full-runtime-dataflow.md`。

该文档用于统一记录当前项目启动/脚本入口，避免新同学与运维在入口上产生歧义。

## 1. 服务入口（HTTP）

### 1.1 安装后命令（首选）

- `mt5services`  
  指向：`src.entrypoint.web:launch`

正式启动门禁：

- `web.launch()`、`instance`、`supervisor` 现在都会先执行 MT5 session gate，再进入 `uvicorn` 或拉起子实例。
- gate 统一要求当前实例的 `mt5.local.ini` 满足：
  - terminal path 可达
  - terminal 进程已预热
  - IPC 已就绪
  - 登录账户与配置账户一致
- 若终端弹密码框或需要人工解锁，入口会直接 fail-fast，并暴露 `interactive_login_required`，而不是继续等待 `IPC timeout`。

对应等价命令：

- `python -m src.entrypoint.web`
- `python -m src.entrypoint.web --help`（当前无参数）
- `python -m src.entrypoint.instance --instance live-main`  
  用于同一代码目录下按 `config/instances/<instance>` 启动指定实例。
- `python -m src.entrypoint.supervisor --environment live`  
  用于按环境显式启动一整组实例；当前 `environment` 与 `topology group` 等价。
- `python -m src.entrypoint.supervisor --group live`  
  用于按 `config/topology.ini` 拉起一个进程组（`main + workers`）。

### 1.2 直接 ASGI 入口（兼容）

- `uvicorn src.api:app --host 0.0.0.0 --port 8808`

该方式不走项目的统一日志/配置加载入口（`src.config.get_system_config` 等），适合调试时使用。
若要做正式 live 启动，仍应先执行：

- `python -m src.ops.cli.live_preflight --environment <live|demo>`

### 1.3 多实例配置目录

- 共享基线配置：`config/*.ini`
- 实例覆盖配置：`config/instances/<instance>/*.ini`
- 实例本地覆盖：`config/instances/<instance>/*.local.ini`
- 当前实例由 `MT5_INSTANCE` 或 `src.entrypoint.instance --instance <name>` 指定
- 当前环境由 `topology group` 决定，并在 supervisor/instance 启动时写入 `MT5_ENVIRONMENT`
- 实例目录只允许承载实例级配置：
  - `mt5.ini`
  - `market.ini`
  - `risk.ini`
- `app.ini`、`db.ini`、`signal.ini`、`topology.ini` 等共享配置不会从实例目录加载
- `mt5.ini` 只描述账户连接信息；环境不再由 MT5 配置反推
- `intrabar_trading.trigger` 与 `app.ini[trading].timeframes` 当前按共享配置统一校验；若 child timeframe 不在全局有效时间框架集合里，`instance/supervisor` 启动会 fail-fast，而不会再允许某些父级 intrabar 运行时永久无数据

推荐形态：

```text
config/
  topology.ini
  instances/
    live-main/
    live-exec-a/
    live-exec-b/
```

### 1.4 启动后最小验证

- 详细步骤请直接对照：`docs/runbooks/system-startup-and-live-canary.md`
- 最小检查点仍然固定为：
  - `data/logs/<instance>/mt5services.log`
  - `data/logs/<instance>/errors.log`
  - `data/runtime/<instance>/`
  - `GET /health`
  - `GET /v1/monitoring/health/ready`

## 2. 交易与运维 CLI 入口

> 当前 CLI 均为模块入口（`python -m module`），尚未统一打包为统一脚本；实际执行请使用同名模块。

### 2.1 回测与分析链路

- `python -m src.backtesting.cli`
- `python -m src.ops.cli.backtest_runner`
- `python -m src.ops.cli.walkforward_runner`
- `python -m src.ops.cli.sltp_grid_search`

### 2.2 指标/行情链路脚本

- `python -m src.ops.cli.backfill_ohlc`
- `python -m src.ops.cli.reset_database`

### 2.3 实验/诊断链路

- `python -m src.ops.cli.mining_runner`
- `python -m src.ops.cli.correlation_runner`
- `python -m src.ops.cli.confidence_check`
- `python -m src.ops.cli.diagnose_no_trades`
- `python -m src.ops.cli.aggression_search`
- `python -m src.ops.cli.exit_experiment`
- `python -m src.ops.cli.live_preflight --environment live`

## 3. 入口职责边界（高层）

- `src/entrypoint/web.py`：日志初始化 + 当前实例 MT5 session gate + `uvicorn.run(target, host, port)`。
- `src/entrypoint/instance.py`：绑定实例名、解析当前环境、刷新实例配置上下文，再进入 `web.launch()`。
- `src/entrypoint/supervisor.py`：读取 `topology.ini`，先做 group/session gate，再按环境/组启动并重启 `main + workers` 进程；每次 child restart 前也会重新检查对应实例的 MT5 gate。
  - §0aa 后约束：supervisor 生命期内任何 child 退出（含 `code=0`）都视为 unexpected——除非 `self._stopping` 已置位。旧 `normal exit → continue` 路径会让已死 process 永驻 `_managed`，被诊断为"永远不重启"。
  - §0bb 后约束：normal-exit 与 unexpected-exit 均会写入独立 alarm counter（`supervisor_unexpected_exit_total{class}`），运维侧可订阅 normal/unexpected 两类 alarm 以快速识别静默缺员。
- `src/backtesting/cli.py` 与 `src/ops/cli/*`：命令参数解析 + 任务编排入口，不承载业务核心算法。
- `src/api/*`：HTTP 适配层，不承担运行时装配与启动职责。
  - 其中交易入口已开始按正式合同收口：
    - `/v1/trade/precheck` 统一返回 `AdmissionReport`
    - `/v1/trade/dispatch` 返回 `ActionResult` 主体及附属 `admission_report`
    - `/v1/trade/traces` 现在会把 `admission_report_appended` 提升成业务解释视图，而不只是裸 pipeline 事件时间线
    - `/v1/trade/state/stream` 已开始直接消费正式 `pipeline_trace_events` 事实源，向前端推送 admission / command / risk / unmanaged-position 关键事件，而不再只依赖本地状态 diff
    - 后台消费链也开始复用同一份 trace 合同：`ExecutionIntentConsumer` 与 `OperatorCommandConsumer` 现在会为 `claim / reclaim / dead-letter / complete / fail` 等生命周期节点补齐统一的 `trace_id / instance / account` 标识，避免 trace 与 SSE 在后台执行阶段丢失业务链上下文
- `src/app_runtime/*`：运行时装配与生命周期。当前已按角色收口为：
  - `main`：构造 `SharedComputeRuntime`（市场采集、指标、信号、calendar sync），并只在显式把 `live_main` 绑定为执行账户时才附带本地 `AccountRuntime`。ADR-010（2026-04-25）后 `build_signal_layer()` 内置 environment-aware 策略过滤——live-main 只装配 `ACTIVE/ACTIVE_GUARDED`，demo-main 额外装配 `DEMO_VALIDATION` 候选
  - `executor`：只构造 `AccountRuntime`，不再在 build 阶段创建 `UnifiedIndicatorManager / SignalRuntime / economic calendar sync`

## 4. 本次同步点

1. 已修正 `pyproject.toml` 的 console entry：

- `mt5services = "src.entrypoint.web:launch"`

2. `AGENTS.md` 中启动说明已更新为当前真实入口。

3. `tests/smoke/test_launch.py` 新增 `__main__` 执行通路测试，覆盖 `python -m src.entrypoint.web` 的行为。
4. 启动后日志位置统一锚定项目根目录 `data/logs/<instance>/`，运行期 SQLite/WAL 统一落到 `data/runtime/<instance>/`。
5. 当前正式多账户部署建议改为“单代码目录 + 多实例配置 + supervisor”，而不是复制多份代码目录。
6. `web` 与 `supervisor` 入口现已统一给每条日志注入 `environment / instance / role`，多实例并行时控制台与文件日志均可直接区分来源。
7. 交易主链路的准入、trace 与状态流已开始收口为正式合同：`AdmissionReport` 负责统一解释“为什么没进交易”，`/v1/trade/traces` 负责把 admission、intent、command、execution 等事件串成业务因果链，`/v1/trade/state/stream` 则开始消费同一份正式 pipeline 事实源；后续执行入口与 worker 自消费链将继续复用同一套 admission/trace 模型。

8. **2026-04-26 启动链生命周期收口（§0aa + §0bb）** — 三个 P1/P2 已修：
   - `src/indicators/engine/pipeline.py`：`shutdown()` 与 `shutdown_global_pipeline()` 现在显式清模块全局 `_global_executor` / `_global_pipeline`。runtime mode 切到 `ingest_only/risk_off` 再切回 `full` 不再拿到已 shutdown 的全局 executor（旧实现"永久毒化"指标主链）。
   - `src/entrypoint/supervisor.py`：移除 `normal exit (code=0) → continue` 路径，所有 child 退出统一进 unexpected 分支 + restart 流程；`_managed` 不再保留已 exit 的 process 引用。
   - `src/ingestion/ingestor.py`：`start()` 现在重建 `_fetch_executor`（旧实现 stop 后再 start 抛 `RuntimeError: cannot schedule new futures after shutdown`）。
   - **AST sentinel**：`tests/utils/test_sentinel_singleton_shutdown_clears_ref.py` 锁住"`_global_X` singleton 若提供 `shutdown_global_X / clear_global_X / close_global_X` helper，函数体必须含 `_global_X = None`"——禁止此类反模式回归。
