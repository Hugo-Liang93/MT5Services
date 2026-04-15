# 高风险整改里程碑（P0 / P1）

> 创建日期：2026-04-11
> 状态对账：2026-04-15
> 目标：针对 `signals / indicators / trading` 主链路，给出可执行的最小化 P0/P1 序列（不做兼容回退式迭代）。

## 进度汇总（2026-04-15 对账）

| 里程碑 | 状态 | 证据 / 残留工作 |
|---|---|---|
| M0.1 入口单一真值源 | ✅ **已闭环** | `pyproject.toml:86` + `web.py:123 launch()` + `tests/smoke/test_launch.py` 3 测试 |
| M0.2 启动配置摘要 | ❌ **未闭环** | `launch()` 只打印 api target/host/port/instance；无策略启用/冻结/intrabar/auto_trade 摘要 |
| M1.1 signals runtime 职责缩口 | ✅ **已闭环** | runtime.py 746 行稳定，方法按 `_apply_policy / _publish / _detect_regime / _apply_filter_chain` 清晰分层 |
| M1.2 indicators 端口化 | ❌ **未闭环** | `bar_event_handler.py` 13 处 `manager._xxx` 私有调用跨文件，明确违反 ADR-006 |
| M1.3 trading 生命周期一致性 | ✅ **已闭环** | intents/commands/execution/positions/pending 全部用 `OwnedThreadLifecycle + is_running()`；ADR-005 由 12 个测试守护 |
| M1.4 观测能力闭环 | ✅ **已闭环** | `intrabar_drop_rate_1m / queue_age_p95_ms / to_decision_latency_p95_ms` 三指标接入告警 + API；`pipeline_trace` + `trade_trace` read model 存在 |
| M1.5 代码体量风控 | ⚠️ **部分** | runtime.py 746 稳定；executor.py 1276 / positions 865 / readmodels/runtime 1523 → 已由 F-6 ~ F-11 覆盖 |

**结论**：原 7 项里程碑 5 项已完成、1 项部分（F-6~F-11 接管）、**2 项仍需收尾**（M0.2 + M1.2）。

---

## 仍需收尾的两项

### M0.2 启动配置摘要（估 0.5 天）

**目的**：让启动日志能一眼回答"这个实例究竟在干什么"——减少"跑着跑着不知道自己跑的是什么配置"的排障成本。

**最小实现**：
- 在 `AppRuntime.start()` 完成后输出一条结构化 INFO：
  ```
  startup_summary: instance=... role=... group=... mode=...
                   auto_trade=true active_strategies=[...] frozen_strategies=[...]
                   intrabar_enabled=... intrabar_strategies=[...]
                   account_bindings={alias: [strategies...]}
  ```
- 数据源：AppContainer 中已有的 `signal_config` / `runtime_identity` / `runtime_mode_controller`，无需新建聚合

**验收**：
- 启动时日志中出现此行
- `admin/strategies` / runtime mode / 该行**三方对账一致**

### M1.2 indicators 端口化（估 1-2 天）

**目的**：ADR-006 合规。`src/indicators/runtime/bar_event_handler.py` 目前对 `UnifiedIndicatorManager` 有 13 处 `manager._xxx` 访问：

```
manager._mark_event_skipped / _mark_event_completed / _mark_event_failed
manager._select_indicator_names_for_history
manager._compute_confirmed_results_for_bars / _compute_intrabar_results_for_bars
manager._write_back_results / _load_confirmed_bars / _group_indicator_values
manager._publish_intrabar_snapshot
```

这些是"handler 函数被 manager 调用，但又反过来回调 manager 私有方法"的循环依赖。不是 ADR-002 允许的"函数化提取 + manager 显式传参"模式。

**方案**：
1. 在 `UnifiedIndicatorManager` 定义 `BarEventPort` Protocol（或直接加 public 方法 `mark_event_skipped / compute_confirmed_results / ...`）
2. bar_event_handler 改为接收该 port 而非 manager
3. 保留现有行为——只是端口重命名 + 去私有下划线
4. 新增测试验证端口稳定性

**验收**：
- `grep "manager\._" src/indicators/runtime/` 返回空
- 指标事件循环仍通过现有 snapshot/event 测试

---

## 后续选择

M0.2 / M1.2 之外的架构演进由 `docs/codebase-review.md` 的 F-6 ~ F-11 跟踪：
- F-6 RuntimeReadModel 1523 行拆分
- F-7 TradeExecutor 1276 行拆分
- F-8 factories/signals.py 1076 行重组
- F-9 TradingModule 1051 行拆分
- F-10 Config 模块职责分离
- F-11 API root `__init__.py` 工厂化

---

## 风险和回退

- 任何阶段若出现行为回归（信号生成率异常、交易下单丢失），先回退到"上一个完整可观测里程碑"；
- 回退标准：入口 smoke + 关键链路 trace（至少 signal/filter/execution）一致。

