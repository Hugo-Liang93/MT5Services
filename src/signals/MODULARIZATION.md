# Signals 模块化目录规划

## 当前已落地分层

```
src/signals/
├── analytics/
│   ├── __init__.py
│   └── diagnostics.py      # 诊断聚合与健康建议（纯计算层）
├── service.py              # 应用编排层（调用 analytics，不承载统计细节）
├── runtime.py              # 事件驱动运行时（后续迁移到 orchestration/）
├── strategies.py           # 策略实现（后续拆分到 strategies/ 目录）
└── ...
```

## 推荐目标结构（下一阶段）

```
src/signals/
├── analytics/              # 观测、诊断、归因
├── orchestration/          # runtime、state machine、voting
├── strategies/             # 每策略一个文件，避免巨型 strategies.py
├── execution/              # position manager / executor adapter
├── contracts/              # signal event schema / enums
└── application/            # service facade + dispatch adapters
```

## 迁移顺序（低风险）

1. **已完成**：analytics 先独立，service 只负责 orchestrate。  
2. 拆分 `strategies.py` 为 `strategies/` 子目录，按策略类型分文件（trend/reversion/breakout）。  
3. 将 `runtime.py` 和 `voting.py` 收敛到 `orchestration/`。  
4. 抽出 `contracts/`（统一 SignalEvent/SignalDecision schema），减少 metadata 漂移。  
5. API 与内部 dispatch 统一走 application 层，避免路由直接耦合底层模块。  

## 可扩展性约定（已支持）

- analytics 支持插件注册表（`AnalyticsPluginRegistry`），可在不改核心代码的前提下新增指标计算逻辑。
- `SignalModule` 支持注入自定义 `DiagnosticsEngine`，便于后续替换质量日报算法（例如引入贝叶斯评分、成本归因模型）。
- 已新增 `signals/orchestration` 兼容入口，作为 runtime/voting/state-machine 迁移的首个落脚点。

## 风险职责边界（重要）

- `src/signals/filters.py`：**信号域过滤**，控制“是否评估/发出信号”。
  - 典型维度：交易时段、价差阈值、经济事件窗口。
- `src/risk/service.py`：**订单域风控**，控制“是否允许下单”。
  - 典型维度：账户快照、仓位限制、保护参数、经济日历策略、降级模式。

结论：两者是分层协作关系，不是重复模块。信号过滤负责降低噪音，风险服务负责最终交易安全。
