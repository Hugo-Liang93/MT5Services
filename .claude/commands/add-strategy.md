引导创建新的信号策略。

根据用户描述的策略逻辑，按照以下 SOP 完成：

1. 确定策略类型（趋势/均值回归/突破/时段/价格行为/复合）
2. 确定所需指标（检查 config/indicators.json 是否已有）
3. 确定 preferred_scopes（confirmed / intrabar+confirmed）
4. 设计 regime_affinity 权重
5. 在对应策略文件中实现类
6. 在 src/signals/strategies/__init__.py 中导出
7. 在 src/signals/service.py 默认策略列表中注册
8. 在 tests/signals/ 中添加单元测试（覆盖四种 Regime）

参考 CLAUDE.md 中的 "Adding New Signal Strategies" 和 docs/architecture/signal-system.md 获取详细规范。
