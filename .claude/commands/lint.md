运行代码质量检查工具链。

依次执行以下检查：

1. `black --check src/ tests/` — 代码格式检查
2. `isort --check-only src/ tests/` — import 排序检查
3. `flake8 src/ tests/` — 代码风格检查

如果发现问题，询问用户是否自动修复（运行 `black` 和 `isort` 不带 `--check`）。

汇报每个工具的结果摘要。
