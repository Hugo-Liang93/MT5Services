"""§0aa R2 sentinel：模块级 ``shutdown_global_*`` / ``clear_global_*`` /
``close_global_*`` 函数必须显式清掉对应 ``_global_*`` 引用。

否则 consumer 调 shutdown 后再 get 时拿到同一已死单例（§0aa P1#1
shutdown_global_executor 教训 + R2 followup shutdown_global_pipeline 同问题）。

本 sentinel 用 AST 扫描 src/，对每个 ``_global_X`` 模块级变量声明，要求
对应的 ``shutdown_global_X`` / ``clear_global_X`` / ``close_global_X`` 函数体
里包含 ``_global_X = None`` 的赋值语句。
"""
from __future__ import annotations

import ast
from pathlib import Path

# 显式豁免清单（理由必须写在注释里）：
_EXEMPT_GLOBALS: dict[str, str] = {
    # _global_cache 是纯内存 LRU，clear() 只清 entries 不销毁实例；
    # 实例本身可重复使用，无需清引用（cache.clear() 是幂等的语义重置）。
    "_global_cache": (
        "smart_cache: clear_global_cache 仅清 entries 不销毁实例，无需 None"
    ),
    # _global_collector 是纯数据 collector（无 thread/executor），单例
    # 可永久持有；没有 shutdown helper 也合理。
    "_global_collector": (
        "metrics_collector: 无 thread/executor，无 shutdown helper 必要"
    ),
    # _global_config_manager 是配置缓存（无 IO 资源），单例可永久持有。
    "_global_config_manager": (
        "indicator_config: 配置缓存无 IO 资源，无 shutdown helper 必要"
    ),
}


def _find_global_singleton_decls(tree: ast.AST) -> list[tuple[str, int]]:
    """找模块级 ``_global_X: Optional[T] = None`` 类型的赋值声明。"""
    found: list[tuple[str, int]] = []
    for node in tree.body if hasattr(tree, "body") else []:
        # _global_X: Optional[T] = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if name.startswith("_global_") and isinstance(node.value, ast.Constant) and node.value.value is None:
                found.append((name, node.lineno))
        # _global_X = None
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name) or not target.id.startswith("_global_"):
                    continue
                if isinstance(node.value, ast.Constant) and node.value.value is None:
                    found.append((target.id, node.lineno))
    return found


def _find_shutdown_func_for(tree: ast.AST, global_name: str) -> ast.FunctionDef | None:
    """找 ``shutdown_global_X / clear_global_X / close_global_X`` 函数。"""
    suffix = global_name.removeprefix("_global_")
    candidates = (
        f"shutdown_global_{suffix}",
        f"clear_global_{suffix}",
        f"close_global_{suffix}",
        f"reset_global_{suffix}",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in candidates:
            return node
    return None


def _func_assigns_none_to(func: ast.FunctionDef, global_name: str) -> bool:
    """检查函数体内是否含 ``_global_X = None`` 赋值。"""
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and node.value.value is None):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == global_name:
                return True
    return False


def test_module_singleton_shutdown_helpers_clear_global_ref() -> None:
    """§0aa R2 sentinel：每个 _global_X 单例若提供 shutdown helper，helper
    必须把 _global_X 置 None 让下次 get 重建（§0aa P1#1 教训根因）。
    """
    src_root = Path(__file__).resolve().parents[2] / "src"
    offenders: list[str] = []

    for path in sorted(src_root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue

        rel = path.relative_to(src_root.parent).as_posix()
        for global_name, lineno in _find_global_singleton_decls(tree):
            if global_name in _EXEMPT_GLOBALS:
                continue
            shutdown_func = _find_shutdown_func_for(tree, global_name)
            if shutdown_func is None:
                # 无 shutdown helper：可能是 thread-bearing 资源裸露（warn）；
                # 但本 sentinel 仅约束"已存在 helper 必须清引用"，无 helper
                # 不视为违规（避免误报纯数据单例）。
                continue
            if not _func_assigns_none_to(shutdown_func, global_name):
                offenders.append(
                    f"  {rel}:{lineno} → {shutdown_func.name}() 未清 {global_name}"
                )

    if offenders:
        formatted = "\n".join(offenders)
        raise AssertionError(
            "§0aa R2 sentinel: 模块级单例 shutdown helper 必须把 _global_X 置 None，"
            "否则 consumer 调 shutdown 后再 get 拿到已死单例（§0aa P1#1 + "
            "shutdown_global_pipeline followup 教训）。\n"
            f"\n命中 ({len(offenders)} 处):\n{formatted}\n\n"
            "若是确不需要清的纯数据单例，请加入 _EXEMPT_GLOBALS 并写明理由。"
        )
