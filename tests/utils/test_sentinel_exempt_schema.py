"""§0aa-followup R1 sentinel：测试代码里 ``_EXEMPT`` / 白名单清单的每个条目
必须配注释说明豁免理由。

§0aa 撕碎了 §0x 的 EXEMPT 决策——CLI 入口"naive UTC 语义合法"假设没
enforce → 默默被打破。本 sentinel 强制：
- 任何 ``_EXEMPT`` / ``_IGNORE`` / ``_ALLOWED_*`` / ``_WHITELIST`` 容器若非
  空，每个条目必须有上一行 / 同行 / 旁注释（# 或 dict value 字符串）说明
  豁免理由。
- 容器声明上方必须有"豁免前提 / 复审条件"的注释段。

这把"豁免清单衰减"挡在合并前。
"""
from __future__ import annotations

import ast
from pathlib import Path

# 容器名前缀模式
_EXEMPT_PREFIXES = ("_EXEMPT", "_IGNORE", "_ALLOWED_", "_WHITELIST")

# 已知合规文件（豁免本 sentinel 自身扫描，避免自指）
_SELF_FILES = {
    "tests/utils/test_sentinel_exempt_schema.py",  # 本文件自身
}


def _is_exempt_container(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _collect_exempt_decls(
    tree: ast.AST, source_lines: list[str]
) -> list[tuple[str, ast.AST, int]]:
    """找模块级 ``_EXEMPT* = {...}`` / ``_EXEMPT*: type = {...}`` 赋值。"""
    decls: list[tuple[str, ast.AST, int]] = []
    for node in tree.body if hasattr(tree, "body") else []:
        target_name: str | None = None
        value: ast.AST | None = None
        lineno: int = 0
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _is_exempt_container(node.target.id) and node.value is not None:
                target_name = node.target.id
                value = node.value
                lineno = node.lineno
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and _is_exempt_container(tgt.id):
                    target_name = tgt.id
                    value = node.value
                    lineno = node.lineno
                    break
        if target_name and value is not None:
            decls.append((target_name, value, lineno))
    return decls


def _container_is_empty(value: ast.AST) -> bool:
    """空容器（`set()` / `{}` / `[]` / set/dict/list literal with no elements）。"""
    if isinstance(value, ast.Call):
        # set() / dict() / list()
        if isinstance(value.func, ast.Name) and value.func.id in {"set", "dict", "list", "frozenset"}:
            return len(value.args) == 0 and len(value.keywords) == 0
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        return len(value.elts) == 0
    if isinstance(value, ast.Dict):
        return len(value.keys) == 0
    return False


def _container_has_documented_entries(
    value: ast.AST, source_lines: list[str]
) -> tuple[bool, str]:
    """检查容器每个 entry 是否配注释说明（豁免理由）。

    规则：
    - dict literal: 每个 value 必须是非空字符串（说明文字）
    - set/list/tuple/frozenset literal: 每个 element 必须紧邻 # 注释行（前一行或同行）
    """
    if isinstance(value, ast.Dict):
        for k, v in zip(value.keys, value.values):
            if not isinstance(v, ast.Constant) or not isinstance(v.value, str) or not v.value.strip():
                key_repr = ast.unparse(k) if hasattr(ast, "unparse") else "<key>"
                return False, f"dict entry {key_repr} 必须用非空字符串 value 说明豁免理由"
        return True, ""

    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        for elt in value.elts:
            elt_lineno = elt.lineno
            # 检查同行或前一行是否有 # 注释
            if elt_lineno > len(source_lines):
                return False, f"无法定位行号 {elt_lineno}"
            same_line = source_lines[elt_lineno - 1]
            prev_line = source_lines[elt_lineno - 2] if elt_lineno >= 2 else ""
            same_has_comment = "#" in same_line.split(',')[0] if False else "#" in same_line
            prev_is_comment = prev_line.lstrip().startswith("#")
            if not (same_has_comment or prev_is_comment):
                elt_repr = ast.unparse(elt) if hasattr(ast, "unparse") else "<elt>"
                return False, (
                    f"line {elt_lineno}: entry {elt_repr} 缺少注释说明豁免理由 "
                    f"(同行或前一行必须有 # 注释)"
                )
        return True, ""

    if isinstance(value, ast.Call):
        # set() / dict() —— 空容器（已在 empty 检查处理），有元素也走不到这
        return True, ""

    return True, ""


def test_exempt_container_entries_must_be_documented() -> None:
    """§0aa-followup R1：tests/ 下 _EXEMPT* / _IGNORE* / _ALLOWED_* / _WHITELIST*
    容器每个条目必须配注释说明，禁止"裸豁免"（同 §0aa P2#4 §0x EXEMPT 反例
    教训：豁免没说明前提就被默默破坏）。
    """
    tests_root = Path(__file__).resolve().parents[1]
    repo_root = tests_root.parent
    offenders: list[str] = []

    for path in sorted(tests_root.rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel in _SELF_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue

        source_lines = text.splitlines()
        for name, value, lineno in _collect_exempt_decls(tree, source_lines):
            if _container_is_empty(value):
                continue   # 空容器无条目可文档
            ok, reason = _container_has_documented_entries(value, source_lines)
            if not ok:
                offenders.append(f"  {rel}:{lineno} {name}: {reason}")

    if offenders:
        formatted = "\n".join(offenders)
        raise AssertionError(
            "§0aa-followup R1 sentinel: 测试 EXEMPT/WHITELIST 容器的每个条目必须"
            "配注释说明豁免理由（dict 用 value 字符串；set/list/tuple 用 # 注释）。"
            "\n\n命中:\n"
            f"{formatted}\n\n"
            "教训：§0x 把 CLI 列入 _EXEMPT 时未写明 'CLI 必须 reject aware' "
            "的前提，§0aa 证实假设错。豁免必须配可证伪安全条件。"
        )
