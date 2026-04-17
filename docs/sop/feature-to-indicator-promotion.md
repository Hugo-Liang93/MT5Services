# SOP：特征晋升为指标（Feature → Indicator Promotion）

> 关联 ADR：[ADR-007 Research 与 Backtesting 的职责边界 + 特征晋升通道](../design/adr.md)
>
> 本文档覆盖两类特征的处理流程：**derived（无需晋升）** 和 **computed（需 4 步手工晋升）**。

## 何时用本 SOP

当 `mining_runner --emit-feature-candidates` 输出带 `[computed]` 标签且 `decision=promote_indicator*` 的候选时，启动本流程。

**不要走本流程**的情况：
- `[derived]` 任何 decision —— 策略可通过现有指标字段直接引用，无需晋升
- `[computed] + decision=research_only` —— 该特征仅用于研究分析，不进入策略管道

## Step 0 — 解读挖掘输出

示例输出：
```
feat_ac46 [robust] [derived]   body_ratio         -> M30 decision=refit
feat_4798 [tf_specific] [computed] bars_in_regime -> M30 decision=research_only
feat_62a0 [tf_specific] [computed] regime_entropy -> M15 decision=research_only
```

决策矩阵：

| feature_kind | decision | 动作 |
|-------------|----------|------|
| derived | 任何 | ✋ **停止晋升**，策略直接引用现有指标字段 |
| computed | research_only | ✋ **停止晋升**，仅供研究 |
| computed | refit | ⏸ 先跑 WF + 跨窗口稳定性验证，通过后再晋升 |
| computed | promote_indicator | ▶ 继续 Step 1 |
| computed | promote_indicator_and_strategy_candidate | ▶ 继续 Step 1，同时准备策略候选 |

## Step 1 — 编写指标实现

### 1.1 选择模块

按特征语义选 `src/indicators/core/` 下文件：

| 目标模块 | 适用类型 |
|---------|---------|
| `volatility.py` | ATR、Bollinger、Keltner、波动率衍生 |
| `momentum.py` | RSI、MACD、Stoch、动量衍生 |
| `mean.py` | MA、EMA 族 |
| `price_structure.py` | swing points、支撑压力、结构位距离 |
| `bar_stats.py` | 蜡烛形态、body/wick 比率 |
| `volume.py` | 成交量衍生 |
| `gap.py`, `composite.py`, `candlestick.py` | 特殊组合 |

### 1.2 函数签名

```python
def your_indicator(
    bars: Sequence[OHLC],
    params: Mapping[str, Any],
) -> Dict[str, Any]:
    """返回 {field_name: value} 字典。

    所有字段名以指标名为前缀，避免跨指标冲突。
    """
    lookback = int(params.get("lookback", 20))
    if len(bars) < lookback:
        return {}
    # ... 计算逻辑
    return {"your_field": value}
```

### 1.3 硬约束

- **禁止 look-ahead**：只能用 `bars[: i+1]`（当前 bar 及之前）
- **缺失值**：返回 `{}` 或字段值为 `None`/`float("nan")`；**不得 raise**
- **字段名前缀**：`your_indicator.your_field`，由 pipeline 自动加前缀
- **无副作用**：纯函数，不写文件/日志/全局状态

## Step 2 — 注册到 `config/indicators.json`

追加新条目：

```json
{
  "name": "your_indicator",
  "module": "src.indicators.core.your_module",
  "function": "your_indicator",
  "params": { "lookback": 20 },
  "dependencies": [],
  "enabled": true
}
```

**约定**：
- `dependencies` 一律填 `[]` — pipeline 自动按字段引用解析指标间依赖
- `enabled: true` 表示主系统启用；可先设 `false` 做灰度
- `params` 必须显式列出所有可调参数（便于回测 grid search）

## Step 3 — 编写测试

在 `tests/indicators/test_your_indicator.py` 添加：

```python
import pytest
from src.clients.mt5_market import OHLC
from src.indicators.core.your_module import your_indicator


class TestYourIndicator:
    def test_basic_computation(self):
        bars = [OHLC(time=..., open=..., high=..., low=..., close=..., volume=...)]
        result = your_indicator(bars, params={"lookback": 20})
        assert result["your_field"] == pytest.approx(expected_value)

    def test_insufficient_data_returns_empty(self):
        bars = []
        assert your_indicator(bars, params={"lookback": 20}) == {}

    def test_no_lookahead(self):
        """第 i 根 bar 的计算结果只依赖 bars[:i+1]，不依赖未来。"""
        # 对 bars 前缀截断后重新计算，结果应与全量时第 i 位结果一致
```

**覆盖要求**：
- 正常场景的数值验证（parametrize 多组输入）
- 边界：空 bars / 1 根 bar / 所有 bar 价格相同 / NaN 输入
- Look-ahead 回归测试（非常重要）

## Step 4 — 触发 pipeline 重算

```bash
# 1. 本地验证
pytest tests/indicators/test_your_indicator.py -v
pytest tests/indicators/  # 确认未 break 现有指标
pytest tests/signals/     # 确认策略未 break
mypy src/indicators/core/your_module.py

# 2. 重启服务（开发环境）
# Windows:
python -m src.entrypoint.instance --instance live-main

# 3. 观察
# - 健康端点确认指标被加载：curl http://localhost:PORT/v1/indicators/status
# - 挖掘端再跑一次，确认 your_indicator.* 能作为新特征被发现
```

## Step 5 — 晋升后验证（7 天观察窗）

新指标上线后 7 天内：

- **`/v1/indicators/diag`** 观察：你的指标 compute latency、NaN 率、吞吐
- **挖掘 rerun**：`mining_runner --emit-feature-candidates` 再跑，对比晋升前后候选变化
- **回归测试**：如果该指标喂给已有策略，跑至少一次全量 backtest 确认 Sharpe/PF 未恶化

## 边界泄漏检查

完成后在 PR 描述列明：

- 新指标是否引入新的跨模块依赖？（禁止直接读/写其他模块 `_` 私有属性）
- 是否触发了 pipeline 的热重载/重启？
- 是否在 `docs/codebase-review.md` 的"2026-XX-XX 指标新增"小节记录？

## 半自动化展望

ADR-007 中期目标：`src/ops/cli/promote_feature.py` 将自动生成 Step 1-3 骨架。当前全手工——手工是**安全的默认**，直到自动化工具经过充分验证。

## 常见错误

| 错误 | 症状 | 排查 |
|------|------|------|
| 未加 indicators.json | pipeline 不计算新指标 | `grep your_indicator config/indicators.json` |
| look-ahead bug | 回测胜率虚高、Paper 不复现 | 用 Step 3 的 no_lookahead 测试 |
| 字段名冲突 | 别的指标字段被覆盖 | 确保字段名有 `indicator_name.field_name` 结构 |
| 返回异常值 | strategy 计算爆 NaN / inf | 关键边界加 `if value is None or not math.isfinite(value): return {}` |
| dependencies 手写 | pipeline 依赖循环或顺序错 | 永远保持 `dependencies: []`，让 pipeline 自解析 |
