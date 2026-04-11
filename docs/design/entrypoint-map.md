# 运行入口映射

> 更新日期：2026-04-11

该文档用于统一记录当前项目启动/脚本入口，避免新同学与运维在入口上产生歧义。

## 1. 服务入口（HTTP）

### 1.1 安装后命令（首选）

- `mt5services`  
  指向：`src.entrypoint.web:launch`

对应等价命令：

- `python -m src.entrypoint.web`
- `python -m src.entrypoint.web --help`（当前无参数）

## 1.2 直接 ASGI 入口（兼容）

- `uvicorn src.api:app --host 0.0.0.0 --port 8808`

该方式不走项目的统一日志/配置加载入口（`src.config.get_system_config` 等），适合调试时使用。

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
- `python -m src.ops.cli.live_preflight`

## 3. 入口职责边界（高层）

- `src/entrypoint/web.py`：只负责日志初始化 + `uvicorn.run(target, host, port)`。
- `src/backtesting/cli.py` 与 `src/ops/cli/*`：命令参数解析 + 任务编排入口，不承载业务核心算法。
- `src/api/*`：HTTP 适配层，不承担运行时装配与启动职责。
- `src/app_runtime/*`：运行时装配与生命周期。  

## 4. 本次同步点

1. 已修正 `pyproject.toml` 的 console entry：

- `mt5services = "src.entrypoint.web:launch"`

2. `AGENTS.md` 中启动说明已更新为当前真实入口。

3. `tests/smoke/test_launch.py` 新增 `__main__` 执行通路测试，覆盖 `python -m src.entrypoint.web` 的行为。


