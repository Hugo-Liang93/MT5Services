# TODO

- 决定人工触发 `POST /v1/trade/closeout-exposure` 成功后，是否自动切换 `runtime_mode`，以及切换到 `risk_off` 还是 `ingest_only`。
- 为 `trade/trace/{signal_id}` 增加按 `trace_id` 直接查询的只读入口，覆盖“被过滤未生成 signal_id”的链路排查场景。
- 继续细化主链路结构化事件模型，补充策略级过滤/风控节点与执行阻断节点的标准化事件分类，避免后续 trace 语义漂移。
- 继续按职责拆分 `TradingModule` 剩余协调逻辑，优先评估 idempotency/replay 是否应独立成正式组件。
