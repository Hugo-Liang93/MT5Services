"""ADR-006 边界 guard：禁止 indicators runtime 层跨模块访问 manager._xxx。

M1.2 在 2026-04-15 收尾后的护栏。之前 bar_event_handler.py 有 13 处
manager._mark_event_*/compute_*/load_*/write_back_*/group_*/publish_* 调用
经由 QueryBindingMixin 动态绑定生效——虽然能运行，但绕过了 ADR-006
"装配层/跨模块禁止访问私有属性"的契约。

改回来风险：下次有人懒得 import 模块级函数，"顺手"又 manager._xxx 补一笔，
整个 ADR 合规状态就破了。此测试直接在源码层扫描防御。
"""

from __future__ import annotations

from pathlib import Path

import pytest

INDICATORS_RUNTIME_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "indicators" / "runtime"
)

# manager_bindings.py 本身就是 "所有私有名在此显式注册" 的桥，属于合规例外
_EXEMPT_FILES = {"manager_bindings.py"}


def _collect_violations() -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for py_file in INDICATORS_RUNTIME_DIR.rglob("*.py"):
        if py_file.name in _EXEMPT_FILES:
            continue
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # 只抓 "manager._xxx" 形式（有下划线 = 私有）
            if "manager._" in line:
                # 排除 docstring / 字符串里的说明性提及
                if '"""' in line or "'''" in line:
                    continue
                violations.append((py_file.name, lineno, stripped))
    return violations


def test_no_private_manager_access_in_indicators_runtime():
    violations = _collect_violations()
    if violations:
        report = "\n".join(
            f"  {fname}:{lineno}: {line}" for fname, lineno, line in violations
        )
        pytest.fail(
            "ADR-006 违反：indicators/runtime 层禁止访问 manager._xxx 私有方法。\n"
            "改用 src/indicators/query_services/runtime.py 的模块级函数 "
            "(mark_event_skipped, compute_confirmed_results_for_bars, ...)：\n"
            f"{report}"
        )
