"""§0di P2 sentinel：mining_runner CLI 必须用 with 块包裹 ResearchDataDeps。

§0cc 已经把 cleanup 端口正式暴露 (build_research_data_deps 支持 close +
context manager)，但 CLI 入口仍然裸构造 deps = build_research_data_deps()
而不走 with —— writer 连接池 + indicator pipeline 线程池在 CLI 路径上持续
泄漏。本 sentinel 锁定 with 包裹模式，防回归。
"""
from __future__ import annotations

import inspect

from src.ops.cli.mining_runner import _run_single


def test_run_single_uses_with_build_research_data_deps() -> None:
    """_run_single 必须用 with build_research_data_deps() as deps 模式。"""
    src = inspect.getsource(_run_single)
    assert "with build_research_data_deps()" in src, (
        "_run_single 必须用 with build_research_data_deps() 包裹（§0di P2）；"
        "否则 writer 连接池 + indicator pipeline 线程池每次 mining CLI 调用都泄漏"
    )
    # 确保 with 语句的位置在 build_research_data_deps() 调用之前一行
    lines_before = src.split("build_research_data_deps()")[0].splitlines()
    assert "with " in lines_before[-1], (
        "build_research_data_deps() 必须在 with 语句中调用，而不是裸赋值；"
        f"当前最后一行 before call: {lines_before[-1]!r}"
    )
