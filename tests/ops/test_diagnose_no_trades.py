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


def test_diagnose_threads_tf_and_hours_into_recent_query() -> None:
    """回归：CLI 暴露 --tf / --hours 但旧实现硬编码 ?symbol=XAUUSD&limit=50，
    完全不传 timeframe / from / to，导致 DB SIGNAL EVENTS 段混入其它 TF
    与更早的记录但被展示成"当前 TF / 最近 N 小时"。

    /v1/signals/recent 现行支持 timeframe + from/to query 参数。
    """
    source = inspect.getsource(diagnose_no_trades)

    # 必须把 args.tf 真实拼进请求（无论 if-else 还是 dict params）
    assert "args.tf" in source, "args.tf 必须被透传到 recent 请求"
    # 必须把 args.hours 真实拼进 from/to 时间范围
    assert "args.hours" in source, "args.hours 必须被透传到 recent 请求"
    # 必须使用 timeframe 与 from / to query 参数名（与路由签名一致）
    assert "timeframe" in source, "应传 timeframe= query 参数"
    # from 是 alias，路由侧 query 名是 'from'；接受 'from=' 字面或 params={'from':...}
    assert (
        '"from"' in source
        or "'from'" in source
        or "from=" in source
    ), "应传 from= query 参数（alias）"
