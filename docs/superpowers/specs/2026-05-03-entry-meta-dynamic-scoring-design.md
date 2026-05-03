# Entry Meta 动态打分端口设计规格

日期：2026-05-03
范围：Research + Backtest overlay；不接入 demo/live runtime；不影响真实下单。

## 1. 背景与问题

当前 Entry Meta-Label Overlay Lab 已经能从 baseline backtest 的 `raw_results.trades`
训练 `take_entry_prob / block_entry_prob`，并在回测中用 `shadow` / `filter`
评估交易增量。

现有 overlay 仍有一个核心限制：它主要按
`bar_time + strategy + direction` 查 artifact 里预计算好的 prediction。这个模式能验证
baseline 中出现过的 entry，但不能对 artifact 中不存在的新 entry 做泛化打分。缺失
prediction 时 overlay 默认放行，报告会出现 `missing_predictions`，这会低估模型过滤能力，
也不接近未来“实时概率计算”的目标。

根因不是 overlay 代码本身，而是 artifact 当前只保存了历史 entry 的预测结果，没有保存一个
可审查、可反序列化、可在回测当下复用的 scoring contract。

## 2. 目标与非目标

目标：

- 新增 Entry Meta 动态打分端口，让 backtest overlay 能对当前 signal entry 计算
  `take_entry_prob / block_entry_prob`。
- 将训练输出从“仅保存历史 predictions”升级为“保存 JSON 可审查 scorer payload +
  历史 predictions”。
- 复用训练时同一套 feature manifest 和 category mappings，避免训练/回测特征漂移。
- 缩小 `missing_predictions` 的含义：只有缺少必要特征、未知类别或 artifact 不支持动态打分时才标记 missing。
- 保持第一阶段仍只进入 Research + Backtest overlay，不进入 demo/live runtime。

非目标：

- 不把模型接入真实下单、demo runtime、live runtime。
- 不引入 pickle/joblib/sklearn 对象反序列化作为 artifact 合同。
- 不在 overlay 中读取 engine、portfolio 或模型私有字段。
- 不重做 State Edge；State Edge 仍是市场状态概率，Entry Meta 仍是交易入场保留概率。
- 不在本阶段解决 pending entry 的真实成交价/成交时间再打分问题；先对 signal entry 当下打分。

## 3. 设计决策

### 3.1 JSON-native scorer

第一阶段使用 JSON-native `logistic_regression_v1` scorer：

- 训练阶段可继续使用 NumPy/scikit-learn 拟合逻辑回归。
- artifact 只保存可审查参数：`feature_keys`、`category_mappings`、`coef`、`intercept`、
  `classes`、`normalization`。
- overlay 加载 artifact 后由 `EntryMetaScorer` 进行纯 Python/NumPy 推理。
- 不保存 sklearn estimator 对象，不使用 pickle/joblib。

保留 `constant_prior` scorer 作为样本不足时的只读 fallback，但 `filter` 仍要求 artifact
status 为 `accepted`，因此 refit artifact 不会参与过滤。

### 3.2 Feature row builder

新增正式 feature row 端口，而不是让 overlay 拼字符串或探测训练 internals：

```text
EntryMetaFeatureRowBuilder
  input: feature_keys, category_mappings, candidate context
  output: EntryMetaFeatureRow | EntryMetaFeatureRowError
```

候选上下文来自 `process_decision()` 已经公开持有的当前事实：

- `bar.time`
- `bar.close` 作为第一阶段 entry price
- `bar_index`
- `decision.strategy`
- `decision.direction`
- `decision.confidence`
- 当前 `indicators` snapshot
- 当前 hard regime
- 当前 session

训练侧仍使用 `EntryMetaFeatureBuilder` 从 DataMatrix 构造全量矩阵。动态侧使用同一组
`feature_keys` 和 `category_mappings` 构造单行特征，确保训练和 overlay 输入同构。

### 3.3 Overlay 调用顺序

`EntryMetaBacktestOverlay.evaluate()` 保留 lookup 能力，并新增 dynamic score：

1. 先查 artifact 预计算 prediction。命中时使用该 prediction，`score_source="artifact_prediction"`。
2. 未命中且调用方提供动态上下文时，使用 `EntryMetaScorer` 打分，`score_source="dynamic_scorer"`。
3. 动态打分失败时默认放行，reason 记录为明确错误类型，例如：
   - `entry_meta_feature_context_missing`
   - `entry_meta_unknown_category`
   - `entry_meta_unsupported_scorer`
   - `entry_meta_feature_missing`

这样不会因为模型覆盖不足而静默挡单，也能把 coverage 问题投影到报告。

### 3.4 Backtest 公开端口

`process_decision()` 只通过公开参数调用 overlay：

```python
entry_meta_overlay.evaluate(
    bar.time,
    decision.strategy,
    decision.direction,
    confidence=decision.confidence,
    feature_context=feature_context,
)
```

`EntryMetaFeatureContext` 是 research/backtest overlay 的正式数据结构，不读取 engine 私有字段。
它只由 `process_decision()` 当前已经拥有的 `bar`、`indicators`、`regime`、`decision` 构造。

## 4. 模块职责

新增或修改：

| 模块 | 职责 |
|---|---|
| `src/research/entry_meta/scoring.py` | 读取 artifact scorer payload，执行 JSON-native 推理 |
| `src/research/entry_meta/features.py` | 增加单行 feature row builder 与 context 数据结构 |
| `src/research/entry_meta/training.py` | 训练并序列化 `logistic_regression_v1` scorer payload |
| `src/research/entry_meta/artifacts.py` | 校验 scorer payload 是 JSON object，不接受任意对象 |
| `src/research/entry_meta/overlay.py` | lookup → dynamic scorer → missing allow 的决策流程 |
| `src/backtesting/engine/signals.py` | 构造 feature context 并通过公开参数传给 overlay |
| `tests/research/entry_meta/` | 覆盖 scorer、row builder、artifact roundtrip、overlay dynamic fallback |
| `tests/backtesting/test_entry_meta_overlay.py` | 覆盖 process_decision 传递动态上下文 |

不修改：

- `src/trading/`
- `src/api/`
- demo/live runtime 启动链路
- 风控和真实下单链路

## 5. Feature Context 合同

建议数据结构：

```python
@dataclass(frozen=True)
class EntryMetaFeatureContext:
    bar_time: datetime | str
    bar_index: int
    strategy: str
    direction: str
    confidence: float
    entry_price: float
    indicators: dict[str, dict[str, Any]]
    regime: str
    session: str
```

`session` 第一阶段允许由 backtest session filter 提供；如果不可得，写入 `"unknown"`。
如果 artifact 的 category mapping 不包含 `"unknown"`，动态打分失败并放行，不做隐式编码。

## 6. Scorer Payload 合同

`model_payload` 新增：

```json
{
  "estimator": "logistic_regression_v1",
  "feature_order": [
    "entry.confidence",
    "entry.direction.buy",
    "entry.direction.sell",
    "entry.price",
    "entry.strategy_code",
    "indicator.atr14.atr",
    "matrix.regime_code",
    "matrix.session_code"
  ],
  "classes": [0, 1],
  "coef": [[0.1, -0.2]],
  "intercept": [0.05],
  "normalization": {
    "mean": [0.0],
    "scale": [1.0]
  },
  "prediction_reuse": "dynamic_scorer"
}
```

约束：

- `feature_order` 必须等于 artifact 顶层 `feature_keys`。
- `coef` 维度必须匹配 feature 数。
- `normalization.scale` 中 0 或非有限值必须被训练阶段替换为 1.0。
- 推理概率必须有限、在 `[0, 1]`，且两类概率和为 1.0。

## 7. 失败边界

必须失败：

- artifact scorer payload 结构非法。
- feature order 与 artifact `feature_keys` 不一致。
- scorer 推理输出非有限或概率不合法。

可降级放行：

- 当前 entry 的 strategy/regime/session 不在训练 category mapping 中。
- 当前 indicator snapshot 缺失 artifact 要求的 indicator field。
- artifact 是旧格式，只支持 lookup predictions。

降级必须进入 overlay report，不允许静默吞掉。

## 8. 报告增强

`entry_meta_overlay.report()` 增加：

- `score_source_counts`
  - `artifact_prediction`
  - `dynamic_scorer`
  - `missing`
- `missing_by_reason`
- `dynamic_scored`
- `dynamic_score_failures`

`entry_meta_overlay_report` 继续使用现有 blocked attribution；新增字段只解释覆盖率，不替代
PnL/PF/expectancy/DD 验收。

## 9. 测试策略

单元测试：

- `EntryMetaFeatureRowBuilder` 能按 artifact feature order 构造单行特征。
- 未知 strategy/regime/session 返回明确错误，不隐式新增编码。
- 缺失 indicator 返回明确错误。
- `EntryMetaScorer` 对 logistic payload 输出合法概率。
- artifact roundtrip 保持 scorer payload。
- overlay 在 lookup miss 后能走 dynamic scorer。
- dynamic scorer 失败时放行并计入 `missing_by_reason`。

集成测试：

- `process_decision()` 将当前 bar、decision、indicators、regime 通过 `feature_context`
  传给 overlay。
- shadow 模式使用 dynamic scorer 不改变交易。
- filter 模式只在 `take_entry_prob < threshold` 且 artifact accepted 时阻断。

回归测试：

- 现有 Entry Meta 聚焦测试必须通过。
- State Edge 回归测试必须通过，确保该改动不破坏 State Edge overlay。

## 10. 成功标准

工程成功：

- lookup-only overlay 升级为 lookup + dynamic scorer。
- 新 artifact 可在 backtest 中对未预计算 entry 打分。
- 报告可以区分 prediction 命中、动态打分、缺失放行。
- 不接入 demo/live，不新增真实交易风险。

研究成功仍以后续 backtest 报告为准：

- shadow 不改变 baseline。
- filter 不得让 PnL、PF、expectancy 退化。
- max DD 不得恶化超过 10%。
- blocked attribution 不得显示挡掉关键大盈利单。
