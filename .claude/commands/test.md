运行项目测试套件。

根据参数执行不同范围的测试：

- 无参数: 运行全部测试 `pytest`
- `unit`: 仅单元测试 `pytest -m unit`
- `fast`: 跳过慢速测试 `pytest -m "not slow"`
- `cov`: 带覆盖率报告 `pytest --cov=src --cov-report=term-missing`
- 指定路径: 运行指定测试文件或目录 `pytest <path>`

运行测试后汇报结果摘要：通过/失败/跳过数量，以及失败测试的简要原因。
