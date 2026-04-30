"""ADR-013 P5: EntryPolicy 架构边界 sentinel 测试。

防止以下回归（一旦命中即测试失败 + 必须修复）：

1. **`_entry_spec` 复活**：14 个结构化策略 + base.py 都不能再定义 `_entry_spec`
   方法。入场决策已上提到 EntryPolicy 端口，不允许策略内部计算入场价。

2. **依赖方向单向**：`src/trading/entry_policy/` 模块不允许 import
   `src.signals.runtime` / `src.trading.pending.manager` / 任何策略实例。
   policy 是纯函数式输入输出，禁止反向访问运行时状态。

3. **`MK.ENTRY_SPEC` 不重新出现**：metadata key 早已删除（ADR-013 P2）。
   反补丁纪律：删除即一次性，不留 deprecation 窗口。

参 [docs/design/adr.md](docs/design/adr.md) ADR-013 + plan §K。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.signals import metadata_keys as mk_module
from src.signals.metadata_keys import MetadataKey

REPO_ROOT = Path(__file__).resolve().parents[2]
STRUCTURED_DIR = REPO_ROOT / "src" / "signals" / "strategies" / "structured"
ENTRY_POLICY_DIR = REPO_ROOT / "src" / "trading" / "entry_policy"

# 14 个结构化策略文件 + base.py（参 plan F.1）
STRATEGY_FILES = [
    "base.py",
    "breakout_follow.py",
    "lowbar_entry.py",
    "mined_rule.py",
    "open_range_breakout.py",
    "price_action_m15.py",
    "pullback_window.py",
    "range_reversion.py",
    "regime_exhaustion.py",
    "session_breakout.py",
    "strong_trend_follow.py",
    "sweep_reversal.py",
    "trend_continuation.py",
    "trendline_touch.py",
]


def _function_names_in(path: Path) -> set[str]:
    """返回文件内所有函数 / 方法名（含 nested）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _imports_in(path: Path) -> set[str]:
    """返回文件内所有 import 的模块全限定名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.add(module)
            for alias in node.names:
                if module:
                    imports.add(f"{module}.{alias.name}")
                else:
                    imports.add(alias.name)
    return imports


# ── 1. _entry_spec 不复活 ────────────────────────────────────────────────────


@pytest.mark.parametrize("filename", STRATEGY_FILES)
def test_entry_spec_method_removed_from_strategy(filename: str) -> None:
    """ADR-013 反补丁纪律：策略禁止定义 `_entry_spec`，入场决策由 EntryPolicy 端口承担。"""
    path = STRUCTURED_DIR / filename
    assert path.exists(), f"strategy file missing: {filename}"
    names = _function_names_in(path)
    assert "_entry_spec" not in names, (
        f"{filename}: `_entry_spec` 已迁移到 EntryPolicy 端口（ADR-013），"
        f"策略不应再定义此方法。请把入场决策放到 src/trading/entry_policy/policies/。"
    )


# ── 2. 依赖方向单向 ─────────────────────────────────────────────────────────


FORBIDDEN_DEPS = (
    "src.signals.runtime",
    "src.signals.orchestration.runtime",
    "src.trading.pending.manager",
    "src.trading.execution.executor",
    "src.signals.service",
)


def _all_python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize(
    "policy_file", [p.name for p in _all_python_files(ENTRY_POLICY_DIR)]
)
def test_entry_policy_does_not_import_runtime_or_manager(policy_file: str) -> None:
    """ADR-006 + ADR-013 边界：policy 是纯函数，禁止反向 import 运行时组件。"""
    matches = [p for p in _all_python_files(ENTRY_POLICY_DIR) if p.name == policy_file]
    assert matches, f"entry_policy file not found: {policy_file}"
    path = matches[0]
    imports = _imports_in(path)
    forbidden_hits = [
        imp for imp in imports for f in FORBIDDEN_DEPS if imp.startswith(f)
    ]
    assert not forbidden_hits, (
        f"{policy_file}: 不允许 import {forbidden_hits}（ADR-013 边界）。"
        f"EntryPolicy 必须是纯函数式输入输出，禁止反向访问 runtime/manager。"
    )


# ── 3. MK.ENTRY_SPEC 不复活 ────────────────────────────────────────────────


def test_metadata_key_entry_spec_removed() -> None:
    """ADR-013 反补丁纪律：MK.ENTRY_SPEC 删除即一次性，不留 deprecation 窗口。"""
    assert not hasattr(MetadataKey, "ENTRY_SPEC"), (
        "MetadataKey.ENTRY_SPEC 已被 ENTRY_INTENT / ENTRY_SPEC_GROUP / "
        "ENTRY_POLICY_DECISION 取代（ADR-013 P2）。请勿恢复此常量——"
        "入场决策契约改为 EntrySpecGroup。"
    )


def test_metadata_key_module_does_not_define_entry_spec_string() -> None:
    """文本层守护：mk_module 源码中不能再出现裸的 `ENTRY_SPEC =` 赋值。"""
    source = Path(mk_module.__file__).read_text(encoding="utf-8")
    # 注意排除 ENTRY_SPEC_GROUP / ENTRY_INTENT 等延伸名（必须严格匹配）
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assert (
                        target.id != "ENTRY_SPEC"
                    ), "metadata_keys.py 不应再定义 ENTRY_SPEC 常量"


# ── 4. base.py 不再实现入场默认 fallback ────────────────────────────────────


def test_structured_base_does_not_define_default_entry_spec() -> None:
    """base.py 默认 `_entry_spec` 已删除（ADR-013 P2 不留 fallback）。"""
    base_path = STRUCTURED_DIR / "base.py"
    names = _function_names_in(base_path)
    assert "_entry_spec" not in names, (
        "base.py 默认 `_entry_spec()` fallback 已删除（ADR-013）。"
        "新需求请在 EntryPolicy registry 注册新 policy。"
    )
