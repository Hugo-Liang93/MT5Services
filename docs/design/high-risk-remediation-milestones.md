# 高风险整改里程碑（P0 / P1）

> 更新日期：2026-04-11  
> 目标：针对 `signals / indicators / trading` 主链路，给出可执行的最小化 P0/P1 序列（不做兼容回退式迭代）。

## P0（不可延后）

### M0.1 启动与入口单一真值源
- 范围：`src.entrypoint.web`、`pyproject.toml`、启动文档、相关 smoke 测试
- 目标：
  - 统一 `mt5services` console script 指向 `src.entrypoint.web:launch`
  - `python -m src.entrypoint.web` 与 `mt5services` 行为一致
  - `AGENTS.md`/`docs/architecture` 启动说明与事实一致
- 验收：
  - 安装后命令能直接启动（dry-run）
  - `tests/smoke/test_launch.py::test_main_block_invoke_launch` 通过

### M0.2 配置与运行状态可追踪基线
- 范围：`src/signals`, `src/trading`, 运行时只读视图
- 目标：
  - 启动后主入口输出中的关键配置摘要可定位：启用策略、冻结状态、运行模式、intrabar 开关
- 验收：
  - API `admin/strategies`、交易状态 trace 与运行状态三方可交叉对账

## P1（可并行推进）

### M1.1 signals 运行时职责缩口
- 范围：`src/signals/orchestration`
- 目标：
  - `runtime.py` 不再混入持久化/投票与状态构建以外的新职责；
  - status、capability、warmup、recovery 都有单一入口方法。
- 里程验收：
  - 文件新增逻辑行数控制在既有子组件内，`runtime.py` 主体逻辑不再膨胀；
  - 事件链路仍维持相同功能（backtest 与 runtime capability 一致性不变）。

### M1.2 indicators 私有状态端口化
- 范围：`src/indicators/query_*`, `snapshot_publisher.py`, `registry_runtime.py`
- 目标：
  - `UnifiedIndicatorManager` 对外状态读取和状态变更仅通过正式方法访问；
  - 避免子模块通过 `manager._xxx` 跨模块读状态。
- 里程验收：
  - 在 `query_read.py`/`query_runtime.py` 中无新增 `._xxx` 直接依赖写法；
  - 添加最小单测验证关键端口读写行为。

### M1.3 trading 生命周期一致性
- 范围：`src/trading/{execution,positions,pending}`
- 目标：
  - `stop/join` 语义统一：线程未结束不清引用；
  - `is_running()` 与真实运行态一致。
- 里程验收：
  - 重入启动/停止场景下无重复启动幽灵线程；
  - shutdown 路径日志可见失败和超时原因。

### M1.4 观测能力闭环
- 范围：`src/indicators/monitoring`, `src/monitoring`, API trace 接口
- 目标：
  - 为 intrabar 路径补齐 `drop_rate_1m`、`queue_age_p95`、`decision_latency_p95`；
  - trade runtime trace 可回放 `signal -> filter -> execution -> result`。
- 里程验收：
  - 3 个核心指标在 5 分钟滑动窗口有观测值；
  - trace 接口对齐 3 条关键阻断场景（过滤、降级、超时）。

### M1.5 代码体量风控（不加新功能）
- 范围：`src/signals/orchestration/runtime.py`, `src/trading/execution/executor.py`, `src/trading/positions/manager.py`
- 目标：
  - 禁止在现有类中新增新逻辑路径，不做“兼容补丁”分支；
  - 每个里程碑只做职责收口 + 端口清理。
- 里程验收：
  - 每次修改附带 `before/after` 文件体量增减说明；
  - 关键行为依赖测试未下降（现有 smoke + 新增入口测试）。

## 阶段交付顺序

1. 先完成 P0（1-2 天）：入口、启动一致性与可观测基线，避免误判。
2. 立刻启动 M1.2、M1.3 并行（约 5 天）：减少主链路耦合风险。
3. 完成 M1.1 后补 M1.4、M1.5（约 7 天）：把监控闭环与职责收口同步。

## 风险和回退

- 任何阶段若出现行为回归（信号生成率异常、交易下单丢失），先回退到“上一个完整可观测里程碑”；
- 回退标准：入口 smoke + 关键链路 trace（至少 signal/filter/execution）一致。

