"""§0w R4 sentinel：禁止 ``fromisoformat(...).replace(tzinfo=...)`` 反模式。

旧反模式对带偏移 ISO 的标签直接改掉而不做时区换算（参 §0w 教训）。
所有 ISO 解析必须走 ``src.utils.timezone.parse_iso_to_utc`` 或显式
`if naive: replace; else: astimezone` 双分支。

本 sentinel 用 AST 而非纯 grep，避免 comment / docstring 假阳性。
"""
from __future__ import annotations

import ast
from pathlib import Path

# §0aa 反例：§0x 把 CLI 入口列为 EXEMPT 是错的——CLI 没有显式 reject aware
# ISO，用户实际可以传 +08:00 偏移字符串导致窗口静默漂移 8h+。所有 CLI 必须
# 走 parse_iso_to_utc。EXEMPT 仅保留下列两类：
#   (a) 内部接口确实不接收用户输入 (e.g. 库内部解析 round-trip 数据)
#   (b) sentinel 自己的源码 (避免文件内的 docstring 示例命中 AST)
# 若新增豁免，必须在测试代码注释中说明豁免理由。
_EXEMPT: set[str] = set()


def _iter_python_sources() -> list[Path]:
    src = Path(__file__).resolve().parents[2] / "src"
    return sorted(p for p in src.rglob("*.py"))


def _find_offenders_in(source: str) -> list[tuple[int, str]]:
    """AST 扫描：找到形如 ``X.fromisoformat(...).replace(tzinfo=...)`` 的实际调用。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "replace":
            continue
        # 关键字参数必须含 tzinfo=...
        if not any(kw.arg == "tzinfo" for kw in node.keywords):
            continue
        # receiver 必须是 fromisoformat(...) 调用
        receiver = func.value
        if not isinstance(receiver, ast.Call):
            continue
        recv_func = receiver.func
        if isinstance(recv_func, ast.Attribute) and recv_func.attr == "fromisoformat":
            try:
                snippet = ast.unparse(node)
            except (AttributeError, TypeError):
                snippet = "<fromisoformat(...).replace(tzinfo=...)>"
            hits.append((node.lineno, snippet))
    return hits


def test_no_new_fromisoformat_replace_tzinfo_pattern() -> None:
    src_root = Path(__file__).resolve().parents[2]
    offenders: list[tuple[str, int, str]] = []
    for path in _iter_python_sources():
        rel = path.relative_to(src_root).as_posix()
        if rel in _EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, snippet in _find_offenders_in(text):
            offenders.append((rel, line_no, snippet))

    if offenders:
        formatted = "\n".join(
            f"  {rel}:{n}: {snippet}" for rel, n, snippet in offenders
        )
        raise AssertionError(
            "§0w R4 sentinel: 检测到 ``fromisoformat(...).replace(tzinfo=...)`` "
            "反模式回归。该模式对带偏移 ISO 直接改时区标签（不做换算），扭曲"
            "绝对时刻。请改走 ``src.utils.timezone.parse_iso_to_utc``，或显式 "
            "``if dt.tzinfo is None: ... else: dt.astimezone(...)``。\n"
            f"\n命中位置 ({len(offenders)} 处):\n{formatted}\n\n"
            "若是 CLI 入口仅接受 naive UTC 字符串，请把路径加入 _EXEMPT 列表"
            "并在该入口注释说明 naive 语义。"
        )
