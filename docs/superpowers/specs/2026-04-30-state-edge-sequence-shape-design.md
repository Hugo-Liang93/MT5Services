# State Edge Sequence Shape Design

日期：2026-04-30

## 目标

把 GPU 用在更适合它的研究任务上：用最近一段 K 线序列学习市场形态，并输出 `long_edge_prob / short_edge_prob / no_trade_prob`。第一阶段仍限定在 research artifact 和 backtest overlay，不接入 demo/live runtime。

该能力分成两层：

1. `sequence_tcn`：用 PyTorch Temporal CNN / TCN 训练序列概率模型。
2. `shape_neighbors`：对当前序列窗口检索历史相似形态，输出 analog 证据和历史 outcome 分布。

二者共享同一套标签与特征防泄漏规则，但职责不同：TCN 负责预测，neighbors 负责解释与审查。

## 可行性判断

可行，但第一版不使用 K 线截图/图片 CNN。原因：

- 截图会引入渲染比例、颜色、坐标轴、压缩等噪声。
- OHLC 数值窗口能直接表达 K 线实体、上下影线、gap、波动率、相对位置。
- 数值窗口更容易防止未来字段泄漏，并能复用 `DataMatrix.barrier_returns_long/short` 的 cost-after 标签。
- 相似形态检索可以直接基于数值 embedding 做，便于回测审查。

## 数据流

```text
DataMatrix
  -> StateEdgeLabelBuilder
  -> StateEdgeFeatureBuilder
  -> SequenceWindowBuilder(window=64)
  -> sequence_tcn trainer
  -> StateEdgeArtifact(predictions + model_payload + feature_manifest)
  -> Backtest StateEdgeBacktestOverlay
```

`shape_neighbors` 使用同一份窗口数据：

```text
SequenceWindowBuilder
  -> ShapeEmbeddingBuilder
  -> nearest neighbors
  -> analog outcome summary
```

## 输入与标签

标签沿用现有 State Edge 标签：

- long：long 方向最优 barrier cost-after return > 0，且不低于 short
- short：short 方向最优 barrier cost-after return > 0，且高于 long
- no_trade：两侧都不正

第一阶段不改标签定义，避免同时改变模型形态和验收口径。

## 特征窗口

默认 `sequence_window = 64`。

窗口样本形状：

```text
[sample_count, window, feature_count]
```

特征来源：

- `StateEdgeFeatureBuilder` 输出的当前/历史可见特征
- hard regime / soft regime / session
- 后续可加入归一化 OHLC candle shape 特征：
  - body ratio
  - upper/lower wick ratio
  - close location in range
  - log return
  - ATR-normalized range

禁止字段：

- `forward`
- `future`
- `barrier`
- `outcome`
- `label`

## 模型

第一版推荐 `sequence_mlp`，因为当前 Windows + RTX 2060 SUPER + PyTorch CUDA 环境中，`Conv1d`/TCN 训练异常慢，而 MLP 的大矩阵乘法能实际吃到 GPU 加速。

`sequence_mlp`：

- flatten `[window, feature_count]`
- Linear 512 + GELU
- Linear 128 + GELU
- Linear 3 -> logits

保留实验性 `sequence_tcn`：

- 1D causal-ish Conv blocks over time
- BatchNorm/LayerNorm
- ReLU/GELU
- global pooling
- linear classifier -> 3 class logits

训练参数：

- `epochs`: 默认 8
- `batch_size`: 默认 512
- `learning_rate`: 默认 1e-3
- `device`: `cpu` 或 `cuda`

GPU 模式必须 CUDA 可用；不可用直接 fail-fast。

## 相似形态检索

第一版 `shape_neighbors` 不参与交易过滤，只输出研究报告：

- 对每个窗口做标准化 embedding
- 支持两种 embedding：
  1. raw flattened normalized OHLC/feature window
  2. TCN hidden vector
- 用 cosine/L2 距离找 Top-K 历史相似窗口
- 汇总 neighbors 的 long/short/no_trade 标签、best barrier return、平均 cost-after return

用途：

- 审查当前概率是否有历史 analog 支撑
- 发现模型高置信但 analog 证据弱的区域
- 后续可做 retrieval-augmented probability calibration

## CLI

扩展 `state_edge_lab`：

```bash
python -m src.ops.cli.state_edge_lab \
  --environment live \
  --tf H1 \
  --start 2026-01-01 \
  --end 2026-04-01 \
  --backend gpu \
  --model-kind sequence_tcn \
  --sequence-window 64 \
  --epochs 8 \
  --batch-size 512 \
  --json-output artifacts/state_edge_sequence/report.json
```

保留 `tabular`：

```bash
--model-kind tabular
```

## 验收

第一阶段按三层验收：

1. 工程 smoke：
   - 合成序列数据 CPU/GPU 都能训练
   - GPU 确实使用 CUDA
   - artifact 能被 backtest overlay 消费

2. GPU 性能：
- 在大样本 `sequence_mlp` benchmark 上 GPU 至少不慢于 CPU
   - 记录训练耗时、推理耗时、显存占用

3. 交易增量：
   - `shadow` 不改变 baseline 交易数/PnL
   - `filter` 必须改善 PF/expectancy 或降低 DD
   - max DD 不得比 baseline 恶化超过 10%

## 风险

- 小样本窗口 GPU 仍可能慢于 CPU；这不是 GPU 失败，而是任务规模不足。
- 形态相似不等于未来收益相似，必须用 cost-after barrier 标签验收。
- 如果直接把 retrieval 分数接入 filter，容易过拟合；第一阶段仅报告，不参与过滤。
- 不接 demo/live，避免模型污染真实下单链路。

## 实施顺序

1. `SequenceWindowBuilder` 测试和实现。
2. `SequenceTCNTrainer` 测试和实现。
3. `shape_neighbors` 相似检索测试和实现。
4. CLI 参数接入。
5. 合成 CPU/GPU benchmark。
6. H1/M30 小窗口 smoke。
7. 文档与风险台账更新。
