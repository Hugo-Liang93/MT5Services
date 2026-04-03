# TODO

- 决定人工触发 `POST /v1/trade/closeout-exposure` 成功后，是否自动切换 `runtime_mode`，以及切换到 `risk_off` 还是 `ingest_only`。
- 将当前 `trade/trace/{signal_id}` 继续向上游扩展到 `市场数据 → 指标计算 → 信号过滤/风控` 节点，补足从信号前到执行后的完整主链路可视化。
- 评估并设计“主链路结构化事件模型”的上游部分，明确哪些节点需要新增独立事件表，哪些继续只保留状态投影，避免把可观测性继续堆成日志文本。
- 继续按职责拆分 `TradingModule` 剩余协调逻辑，优先评估 idempotency/replay 是否应独立成正式组件。
