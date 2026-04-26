"""Regression tests for src/ops/cli/diagnose_no_trades.py — DB signal endpoint.

历史 bug: requests /v1/signals/latest（已删除路由）→ 异常被 print('Failed
to query: ...') 吞掉 → DB signal-events 视角缺失 → 排障误判到下游。

注意：diagnose_no_trades 是 imperative print 脚本（无可单元测的函数），
故仅用 source-level sentinel test 锁定路径回归。

如果未来把 DB signal events 段抽成独立函数，应改为 monkeypatch requests
+ stdout 捕获的更强测试。
"""

from __future__ import annotations

import inspect

from src.ops.cli import diagnose_no_trades


def test_diagnose_uses_recent_not_latest_signals_endpoint() -> None:
    """回归：禁止再请求 /v1/signals/latest（已删），必须用 /v1/signals/recent。"""
    source = inspect.getsource(diagnose_no_trades)
    assert (
        "/v1/signals/latest" not in source
    ), "/v1/signals/latest 已不存在路由；请用 /v1/signals/recent"
    assert (
        "/v1/signals/recent" in source
    ), "应请求 /v1/signals/recent（catalog.py:121 现行 endpoint）"
