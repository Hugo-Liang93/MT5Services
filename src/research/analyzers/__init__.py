"""信号挖掘分析器包。

启动时自动注册 3 个内置 analyzer 到 protocol 模块的注册表。
未来加新 analyzer：实现 Analyzer Protocol 后调 register_analyzer 即可。
"""

from .default_analyzers import register_default_analyzers
from .protocol import Analyzer, all_analyzer_names, get_analyzer, register_analyzer

# 启动时注册内置 analyzer（idempotent — protocol.register_analyzer 重名覆盖）
register_default_analyzers()

__all__ = [
    "Analyzer",
    "all_analyzer_names",
    "get_analyzer",
    "register_analyzer",
    "register_default_analyzers",
]
