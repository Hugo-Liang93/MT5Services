"""§0aa P2 回归：backtesting CLI 不能用 fromisoformat().replace(tzinfo=UTC)
处理用户传入的 ISO 时间——会把带偏移的 aware ISO 改标签而非做时区换算，
导致回测/优化窗口静默漂移 8h+。

§0x EXEMPT 清单中的"CLI 入口仅接受 naive UTC"假设是错的——CLI 没有显式
reject aware 输入，用户实际可以传任意 ISO。所有 CLI 都必须走 parse_iso_to_utc。
"""
from __future__ import annotations

from datetime import datetime, timezone


def test_backtest_cli_does_not_use_unsafe_fromisoformat_replace_tzinfo() -> None:
    """AST sentinel 全 src/backtesting/cli.py 扫描，禁止 fromisoformat().replace(tzinfo=...) 反模式。

    §0aa 反例：§0x 把该文件加入 EXEMPT 是错的——用户传 aware ISO 时静默算错窗口。
    所有 CLI 必须走 parse_iso_to_utc 走 astimezone 保持绝对时刻。
    """
    import ast
    from pathlib import Path

    cli_path = Path(__file__).resolve().parents[2] / "src" / "backtesting" / "cli.py"
    text = cli_path.read_text(encoding="utf-8")
    tree = ast.parse(text)

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "replace":
            continue
        if not any(kw.arg == "tzinfo" for kw in node.keywords):
            continue
        receiver = func.value
        if not isinstance(receiver, ast.Call):
            continue
        recv_func = receiver.func
        if isinstance(recv_func, ast.Attribute) and recv_func.attr == "fromisoformat":
            offenders.append(node.lineno)

    assert offenders == [], (
        f"backtesting/cli.py 必须走 parse_iso_to_utc，禁止 "
        f"fromisoformat().replace(tzinfo=UTC) 反模式（§0aa P2：CLI 用户可传"
        f"aware ISO，旧实现会改标签而不是换算 → 窗口静默漂移）。"
        f"\n命中行号：{offenders}"
    )


def test_parse_iso_to_utc_handles_cli_aware_input_correctly() -> None:
    """直接验证 parse_iso_to_utc 对 CLI 典型 aware 输入的正确性。

    User 报：2026-04-26T08:00:00+08:00 必须解析为 UTC 2026-04-26T00:00:00。
    """
    from src.utils.timezone import parse_iso_to_utc

    aware_input = "2026-04-26T08:00:00+08:00"
    result = parse_iso_to_utc(aware_input)
    expected = datetime(2026, 4, 26, 0, 0, 0, tzinfo=timezone.utc)
    assert result == expected, (
        f"+08:00 输入必须做 astimezone 转 UTC 绝对时刻，不能改标签；"
        f"got {result.isoformat()!r}, expected {expected.isoformat()!r}"
    )
