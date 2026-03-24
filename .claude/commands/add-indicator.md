引导创建新的技术指标。

根据用户描述的指标逻辑，按照以下步骤完成：

1. 在 src/indicators/core/ 中实现计算函数（签名: `func(bars, params) → dict`）
2. 在 config/indicators.json 中添加条目：
   - `name`: 指标唯一名称
   - `func_path`: 函数路径
   - `params`: 参数字典
   - `dependencies`: 一律填 `[]`
   - `compute_mode`: standard / incremental
   - `enabled`: true/false
   - (可选) `delta_bars`: 如 `[3, 5]` 自动计算 N-bar 变化率
3. 在 tests/indicators/ 中添加测试

参考 CLAUDE.md 中的 "Adding New Indicators" 和现有指标实现（如 src/indicators/core/mean.py）。
