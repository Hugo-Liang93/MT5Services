# TODO

- 决定人工触发 `POST /v1/trade/closeout-exposure` 成功后，是否自动切换 `runtime_mode`，以及切换到 `risk_off` 还是 `ingest_only`。
- 设计并落地“交易主链路可视化”方案：覆盖 `市场数据 → 指标 → 信号 → 过滤/风控 → 执行 → 挂单/持仓` 的结构化节点与状态投影。
- 为交易主链路补统一 trace / correlation 方案，确保可以按 `signal_id / request_id / order_ticket / position_ticket` 串联审查整条链路。
- 评估并设计交易主链路事件模型，明确哪些节点应该落结构化事件，哪些只保留状态投影，避免把可观测性继续堆成日志文本。
- 继续按职责拆分 `TradingModule` 剩余协调逻辑，优先评估 idempotency/replay 是否应独立成正式组件。
