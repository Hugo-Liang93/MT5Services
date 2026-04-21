"""TimescaleWriter repo @property 单例 + startup ensure_schema 回归测试。

历史教训（2026-04-20 生产事故）：
  - `src/api/{research,experiment,backtest}_routes/*.py` 在请求处理路径里
    `TimescaleWriter(settings, min_conn=1, max_conn=2)` + `repo.ensure_schema()`。
  - 每个请求各自起一个 pg 连接池（2 连接）+ 执行 DDL，22:44 内 11 秒重建 8 次。
  - 引发 pg 连接耗尽 → consumer thread 吞异常死亡 → 服务僵尸。
修复契约：
  - `TimescaleWriter.research_repo / experiment_repo / backtest_repo` 是 lazy @property，
    返回同一实例（单 pool 内复用）。
  - `StorageWriter.ensure_schema_ready()` 在启动时一次性调所有 repo 的 ensure_schema。
  - API 路由通过 `deps.get_research_repo` 等走共享实例，禁止 new TimescaleWriter。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.persistence.db import TimescaleWriter
from src.persistence.repositories import BacktestRepository
from src.persistence.repositories.experiment_repo import ExperimentRepository
from src.persistence.repositories.research_repo import ResearchRepository


def _fresh_writer() -> TimescaleWriter:
    """构造一个未连接的 TimescaleWriter（跳过 _init_pool）。"""
    writer = TimescaleWriter.__new__(TimescaleWriter)
    writer.settings = SimpleNamespace(
        pg_host="x",
        pg_port=5432,
        pg_user="x",
        pg_password="x",
        pg_database="x",
        pg_schema="public",
    )
    writer._pool = None
    writer._min_conn = 1
    writer._max_conn = 10
    writer._last_health_check = 0
    writer._health_check_interval = 60
    import threading

    writer._reconnect_lock = threading.Lock()
    for attr in (
        "_market_repo",
        "_signal_repo",
        "_execution_intent_repo",
        "_trade_command_repo",
        "_operator_command_repo",
        "_trading_state_repo",
        "_economic_repo",
        "_pipeline_trace_repo",
        "_runtime_repo",
        "_paper_trading_repo",
        "_backtest_repo",
        "_research_repo",
        "_experiment_repo",
    ):
        setattr(writer, attr, None)
    return writer


def test_backtest_repo_property_is_lazy_singleton() -> None:
    writer = _fresh_writer()
    a = writer.backtest_repo
    b = writer.backtest_repo
    assert isinstance(a, BacktestRepository)
    assert a is b, "backtest_repo 必须 lazy + 同一实例，避免每次访问都重建"


def test_research_repo_property_is_lazy_singleton() -> None:
    writer = _fresh_writer()
    a = writer.research_repo
    b = writer.research_repo
    assert isinstance(a, ResearchRepository)
    assert a is b


def test_experiment_repo_property_is_lazy_singleton() -> None:
    writer = _fresh_writer()
    a = writer.experiment_repo
    b = writer.experiment_repo
    assert isinstance(a, ExperimentRepository)
    assert a is b


def test_storage_writer_ensure_schema_ready_calls_all_repo_schemas() -> None:
    """启动时一次性 ensure_schema，禁止 API 路由运行时再触发 DDL。"""
    from src.persistence.storage_writer import StorageWriter

    sw = StorageWriter.__new__(StorageWriter)
    # 把 db 替换成 MagicMock —— 观察方法调用
    mock_db = MagicMock()
    sw.db = mock_db  # type: ignore[attr-defined]
    StorageWriter.ensure_schema_ready(sw)

    mock_db.init_schema.assert_called_once()
    mock_db.backtest_repo.ensure_schema.assert_called_once()
    mock_db.research_repo.ensure_schema.assert_called_once()
    mock_db.experiment_repo.ensure_schema.assert_called_once()
