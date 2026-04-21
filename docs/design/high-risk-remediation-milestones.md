# 高风险整改里程碑（P0 / P1）

> **📦 状态：已归档（全部闭环，2026-04-15）**
>
> 本文件是 2026-04-11 提出的 **7 项主链路 P0/P1 整改里程碑** 的执行记录。
> 到 2026-04-15 对账时 **6 项已完成 + 1 项部分由 F-6~F-11 接管**，**不再作为进行中任务**。
> 保留作为执行过程的证据记录，未来若需类似整改应**另开新文件**（按日期命名，不覆盖本文件）。
>
> **当前架构演进任务跟踪见**：[`codebase-review.md`](../codebase-review.md) F-6 ~ F-11 段。
>
> 创建日期：2026-04-11 · 状态对账：2026-04-15 · 归档：2026-04-21
> 原目标：针对 `signals / indicators / trading` 主链路，给出可执行的最小化 P0/P1 序列（不做兼容回退式迭代）。

## 进度汇总（2026-04-15 对账）

| 里程碑 | 状态 | 证据 / 残留工作 |
|---|---|---|
| M0.1 入口单一真值源 | ✅ **已闭环** | `pyproject.toml:86` + `web.py:123 launch()` + `tests/smoke/test_launch.py` 3 测试 |
| M0.2 启动配置摘要 | ✅ **已闭环 2026-04-15** | `AppRuntime._log_startup_summary()` 输出 instance/env/role/mode/auto_trade/intrabar/strategies/bindings；`tests/app_runtime/test_startup_summary.py` 4 测试守护 |
| M1.1 signals runtime 职责缩口 | ✅ **已闭环** | runtime.py 746 行稳定，方法按 `_apply_policy / _publish / _detect_regime / _apply_filter_chain` 清晰分层 |
| M1.2 indicators 端口化 | ✅ **已闭环 2026-04-15** | `bar_event_handler.py` 10 处 `manager._xxx` 全部改用 `query_services/runtime.py` 模块级函数；`tests/indicators/test_adr_006_boundary.py` 防回归 guard |
| M1.3 trading 生命周期一致性 | ✅ **已闭环** | intents/commands/execution/positions/pending 全部用 `OwnedThreadLifecycle + is_running()`；ADR-005 由 12 个测试守护 |
| M1.4 观测能力闭环 | ✅ **已闭环** | `intrabar_drop_rate_1m / queue_age_p95_ms / to_decision_latency_p95_ms` 三指标接入告警 + API；`pipeline_trace` + `trade_trace` read model 存在 |
| M1.5 代码体量风控 | ⚠️ **部分** | runtime.py 746 稳定；executor.py 1276 / positions 865 / readmodels/runtime 1523 → 已由 F-6 ~ F-11 覆盖 |

**结论**：原 7 项里程碑 **6 项已完成、1 项部分**（F-6~F-11 接管）。2026-04-11 P0/P1 里程碑**全部闭环**。

---

## 收尾记录

### M0.2 启动配置摘要（2026-04-15 完成）

- 实现：[`src/app_runtime/runtime.py`](../../src/app_runtime/runtime.py) `_log_startup_summary()`，在 `start()` 完成后输出单行 `startup_summary`
- 数据源：`container.runtime_identity` + `runtime_mode_controller.current_mode()` + `signal_config_loader()`
- 输出字段：`instance` / `env` / `role` / `mode` / `auto_trade` / `intrabar_enabled` / `active_strategies` / `intrabar_strategies` / `account_bindings`
- 错误降级：loader 抛异常 → 字段降为 `?`/`[]`/`{}`，不炸启动
- 测试：[`tests/app_runtime/test_startup_summary.py`](../../tests/app_runtime/test_startup_summary.py) 4 用例（正常 / signal_config / loader 失败 / mode 失败）

### M1.2 indicators 端口化（2026-04-15 完成）

- 问题：`bar_event_handler.py` 10 处 `manager._xxx` 跨模块私有访问，通过 `QueryBindingMixin` 动态绑定生效但绕过 ADR-006
- 修复：全部替换为 `query_services/runtime.py` 已存在的模块级函数（`mark_event_skipped` / `mark_event_completed` / `mark_event_failed` / `select_indicator_names_for_history` / `compute_confirmed_results_for_bars` / `compute_intrabar_results_for_bars` / `load_confirmed_bars` / `write_back_results` / `group_indicator_values` / `publish_intrabar_snapshot`）
- 零行为改变：模块级函数本身就是 mixin 动态绑定的内部实现，只是显式调用它而不是绕 manager 一圈
- 防回归：[`tests/indicators/test_adr_006_boundary.py`](../../tests/indicators/test_adr_006_boundary.py) 源码扫描测试，下次有人写 `manager._xxx` 就红
- 验证：`tests/indicators/ 118/118 + 更广 676/676 全通过`

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

