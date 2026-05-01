# State Edge Responsibility Boundaries Design

日期：2026-05-02

## 背景

GPU Research Lab 已经把 State Edge 概率模型接入到 Research artifact 与 Backtest overlay，但当前实现仍容易让两个概念混在一起：

- `cpu/gpu` 是计算后端，不应该成为两套业务链路。
- `tabular/sequence/shape_neighbor` 是模型能力，不应该被后端选择隐式决定。
- CLI 当前承担了训练、质量门、回测与报告编排，后续继续扩展会导致职责膨胀。

本设计目标是把职责边界收口清楚，让后续实施时知道哪个模块负责什么、哪些实现属于冗余、哪些能力应保留为实验分支。

## 目标

1. 明确 State Edge 从数据到回测 overlay 的模块边界。
2. 将模型选择与计算后端解耦。
3. 明确 CPU 与 GPU 的合理职责，避免双轨业务逻辑。
4. 标记当前冗余与实验性实现，为后续清理提供顺序。
5. 保持第一阶段仍限定在 Research + Backtest overlay，不接入 demo/live runtime。

## 非目标

- 不改变真实交易链路。
- 不把 State Edge artifact 接入 demo/live。
- 不在本阶段重写 DataMatrix、FeatureHub 或 BacktestEngine 主体。
- 不为了兼容历史行为保留长期双轨语义。

## 模块边界

### 1. 数据事实层

拥有方：`src/research/core/`

职责：

- 提供 `DataMatrix`、OHLC、indicator series、barrier outcome、train/test slice。
- 作为标签与特征构造的唯一输入事实源。

明确不做：

- 不训练模型。
- 不判断交易准入。
- 不保存 State Edge 预测结果。

### 2. 标签层

拥有方：`src/research/state_edge/labels.py`

职责：

- 复用 `DataMatrix.barrier_returns_long/short`。
- 生成 `long / short / no_trade` 三分类标签。
- 输出 label summary 与每个方向的 best cost-after return。

明确不做：

- 不重新模拟 barrier。
- 不读取 `forward_return`。
- 不参与模型训练和交易过滤。

### 3. 特征层

拥有方：

- `src/research/state_edge/features.py`
- `src/research/state_edge/sequence.py`

职责：

- 构造当前与历史可见的 tabular feature matrix。
- 构造 K 线窗口序列 `[sample, window, feature]`。
- 生成 feature manifest，声明字段来源与防泄漏规则。

明确不做：

- 不读取 future、forward、barrier outcome、label 字段作为特征。
- 不决定模型类型。
- 不决定 CPU/GPU。

### 4. 模型层

建议拥有方：

- `src/research/state_edge/models/`

建议拆分：

- `tabular_baseline.py`
- `sequence_mlp.py`
- `shape_neighbor.py`
- `experimental_sequence_tcn.py`

职责：

- 定义模型结构与训练算法。
- 输入特征与标签，输出概率、模型 payload、训练状态。

明确不做：

- 不读取配置文件。
- 不跑 backtest。
- 不直接写 artifact 文件。
- 不直接判断上线与否。

### 5. 计算后端层

拥有方：`src/research/state_edge/backends.py`

职责：

- 提供 `ComputeBackend(cpu/gpu)`。
- 检查 CPU/GPU 可用性。
- GPU 模式 fail-fast，不静默回退 CPU。
- 提供 readiness benchmark 与诊断信息。

明确不做：

- 不决定使用 `tabular` 还是 `sequence_mlp`。
- 不持有交易逻辑。
- 不实现模型业务语义。

设计约束：

```text
model_kind 决定“学什么”
backend 决定“在哪里算”
```

### 6. 训练服务层

建议拥有方：

- `src/research/state_edge/trainer.py`

职责：

- 接收 `DataMatrix + model_kind + backend + training params`。
- 调用标签层、特征层、模型层。
- 组装 `StateEdgeArtifact`。

明确不做：

- 不跑 baseline/shadow/filter backtest。
- 不写复杂 CLI 编排。
- 不接 demo/live。

### 7. Artifact 层

拥有方：`src/research/state_edge/artifacts.py`

职责：

- 定义 `StateEdgeArtifact`、`StateEdgePrediction`。
- 保存与读取 artifact JSON。
- 固化 feature manifest、label summary、metrics、predictions、model payload。

明确不做：

- 不依赖 sklearn 或 torch 私有对象。
- 不解释交易好坏。
- 不重新计算特征。

### 8. 质量门层

拥有方：`src/research/state_edge/quality.py`

职责：

- 在昂贵 backtest 前判断 artifact 是否值得继续评估。
- 检查 OOS 样本数、top bucket 样本数、label class 样本数。
- 输出 `accepted/refit/rejected`。

明确不做：

- 不替代最终 PF、expectancy、max DD 验收。
- 不直接决定上线。

### 9. 回测 Overlay 层

拥有方：`src/backtesting/state_edge_overlay.py`

职责：

- 只读消费 artifact。
- 提供 `shadow/filter` 两种 backtest 模式。
- 按方向概率过滤入场。
- 输出 overlay 覆盖率、放行数、阻断数、平均概率。

明确不做：

- 不接 demo/live runtime。
- 不解析模型 payload。
- 不读取训练后端。
- 不访问 backtest 私有字段。

### 10. 实验编排层

建议拥有方：

- `src/research/state_edge/experiments.py`

职责：

- 串联 `train -> quality -> baseline -> shadow -> filter -> report`。
- 管理 checkpoint 与阶段状态。
- 支持单 TF 与多 TF 独立验收。

明确不做：

- 不解析命令行参数。
- 不实现模型训练细节。
- 不持有 BacktestEngine 内部逻辑。

### 11. CLI 层

拥有方：

- `src/ops/cli/state_edge_lab.py`
- `src/ops/cli/state_edge_sequence_eval.py`
- `src/ops/cli/backtest_runner.py`

职责：

- 解析参数。
- 调用正式服务。
- 输出 JSON。
- 将错误清晰暴露给操作者。

明确不做：

- 不承载核心实验状态机。
- 不复制训练、质量门或 overlay 逻辑。

### 12. 未来 Runtime 推理层

建议拥有方：

- `src/research/state_edge/inference.py` 或后续独立 runtime adapter。

职责：

- 在下一阶段提供实时 feature snapshot 到概率预测。
- 第一版只做 shadow record。

明确不做：

- 第一阶段不改变真实下单。
- 不使用 GPU 作为 live 必选依赖。

## CPU/GPU 职责

### CPU 保留职责

- 单元测试与 smoke。
- 小窗口排障。
- tabular baseline。
- backtest overlay。
- artifact quality gate。
- 未来实时推理第一版候选。

### GPU 保留职责

- `sequence_mlp` K 线窗口训练。
- 多 TF、多窗口、多参数批量训练。
- walk-forward 重复训练。
- 后续 embedding / shape similarity 大规模检索。

### 不推荐职责

- GPU 不应承担 demo/live 必选路径。
- GPU 不应为了 tabular 小模型强行接入。
- CPU/GPU 不应各自维护一套不同语义的 State Edge 业务链路。

## 冗余与收口清单

### 保留

| 项 | 结论 | 原因 |
|---|---|---|
| `CPUBackend` | 保留 | 稳定基线、测试、排障、未来实时推理候选 |
| `GPUBackend` | 保留 | 序列模型与批量训练需要 |
| `StateEdgeBacktestOverlay` | 保留 | 是 Research 到 Backtest 的正式只读端口 |
| `StateEdgeQualityGate` | 保留 | 避免无效 artifact 进入昂贵回测 |

### 收口

| 项 | 处理 | 原因 |
|---|---|---|
| `training.py` 与 `sequence_training.py` 中重复 metrics/helper | 合并到共享评估模块 | 避免不同训练路径产生不同指标语义 |
| `state_edge_sequence_eval.py` 中的实验状态机 | 下沉到 `experiments.py` | CLI 不应承载核心编排 |
| `model_kind` 与 `backend` 混合判断 | 改为模型层与后端层解耦 | 避免硬件选择改变业务语义 |

### 降级为实验

| 项 | 处理 | 原因 |
|---|---|---|
| GPU tabular `torch_linear` | 标记 experimental 或从主入口移除 | 与 CPU HistGradientBoosting 不是同一模型，容易混淆比较 |
| `sequence_tcn` | 保留 experimental | 当前 Windows + CUDA Conv1d 表现慢，不适合主线 |
| `neighbors.py` | 保留 research-only | 有解释价值，但第一阶段不参与 filter 决策 |

## 推荐实施顺序

1. 保持当前 H1 long-only overlay 验收优先级不变。
2. 把 `model_kind` 与 `backend` 的职责写入配置和文档。
3. 新增 `models/` 模块，把 tabular、sequence_mlp、experimental_tcn 拆开。
4. 抽出共享 metrics/probability helper。
5. 新增 `experiments.py`，把 sequence eval CLI 中的编排逻辑下沉。
6. 将 GPU tabular 从主推荐路径降级为 experimental。
7. 更新 `docs/research-system.md` 与 `docs/codebase-review.md`，记录职责收口与冗余清理。

## 验收标准

工程验收：

- `model_kind` 与 `backend` 可以独立选择，且语义清晰。
- CLI 只负责参数适配，不再承载核心实验状态机。
- overlay 不解析模型 payload。
- GPU 不可用时 `--backend gpu` fail-fast。

交易验收：

- State Edge 是否有价值仍以 backtest overlay 的 PF、expectancy、cost-after return、max DD 判断。
- GPU 只证明计算加速，不自动等于策略增益。

文档验收：

- 文档中明确 CPU/GPU 是同一研究链路的后端，不是两套系统。
- 文档中明确哪些能力是主线、哪些是 experimental。

