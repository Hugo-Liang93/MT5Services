"""CLI 持久化 helper — 让 ops/cli 工具能按需把结果写入 DB。

设计约束（遵循 ADR-008 精神）：
  - 这里的 `TimescaleWriter(settings)` 是**短生命周期**（一次 CLI 运行）。
    ADR-008 的禁令针对长期运行的 API 路由里每请求 `new writer`，
    CLI 一次性 open → save → close 不会引起 pool 风暴。
  - helper 负责 writer 生命周期（try/finally + `closeall`），调用方零配置。
  - 失败不 raise（CLI 结果本已在 stdout/JSON 输出），仅 warning 日志。

调用方约定：
  - `persist_mining_results({"H1": mining_result, "M30": ...}, env, exp_id)`
  - `persist_backtest_result(backtest_result, env, exp_id)`
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Optional

if TYPE_CHECKING:
    from src.backtesting.models import BacktestResult
    from src.persistence.db import TimescaleWriter
    from src.research.core.contracts import MiningResult

logger = logging.getLogger(__name__)


@contextmanager
def _writer_scope(environment: str) -> "Iterator[TimescaleWriter]":
    """打开 TimescaleWriter，use-once 并在退出时关闭连接池。

    捕获所有异常记录 warning 并 re-raise —— 调用方负责处理。
    """
    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter

    settings = load_db_settings(environment)
    writer = TimescaleWriter(settings=settings)
    try:
        yield writer
    finally:
        pool = getattr(writer, "_pool", None)
        if pool is not None:
            try:
                pool.closeall()
            except Exception:
                logger.debug("Failed to close CLI writer pool", exc_info=True)


def persist_mining_results(
    results_by_tf: Mapping[str, "MiningResult"],
    *,
    environment: str,
    experiment_id: Optional[str] = None,
) -> int:
    """把跨 TF 的 MiningResult 逐个入库。

    Args:
        results_by_tf: {timeframe: MiningResult}
        environment: "live" / "demo"
        experiment_id: 若非 None，会写到每个 result 上（贯穿 ADR-007 实验链路）

    Returns:
        成功写入的记录数；失败仅 warning，不 raise。
    """
    if not results_by_tf:
        return 0
    saved = 0
    try:
        with _writer_scope(environment) as writer:
            repo = writer.research_repo
            for tf, result in results_by_tf.items():
                if experiment_id is not None:
                    try:
                        result.experiment_id = experiment_id
                    except Exception:
                        logger.debug(
                            "Cannot set experiment_id on MiningResult for %s", tf
                        )
                try:
                    repo.save_mining_result(result)
                    saved += 1
                    logger.info(
                        "Mining result persisted: tf=%s run_id=%s",
                        tf,
                        result.run_id,
                    )
                except Exception:
                    logger.warning(
                        "Failed to persist mining result for tf=%s run_id=%s",
                        tf,
                        getattr(result, "run_id", "?"),
                        exc_info=True,
                    )
    except Exception:
        logger.warning(
            "Failed to open writer for mining persistence (env=%s)",
            environment,
            exc_info=True,
        )
    return saved


def persist_backtest_result(
    result: "Optional[BacktestResult]",
    *,
    environment: str,
    experiment_id: Optional[str] = None,
) -> bool:
    """把 BacktestResult 入库。失败仅 warning，不 raise。"""
    if result is None:
        return False
    try:
        with _writer_scope(environment) as writer:
            if experiment_id is not None:
                try:
                    result.experiment_id = experiment_id
                except Exception:
                    logger.debug("Cannot set experiment_id on BacktestResult")
            writer.backtest_repo.save_result(result)
            logger.info("Backtest result persisted: run_id=%s", result.run_id)
            return True
    except Exception:
        logger.warning(
            "Failed to persist backtest result run_id=%s (env=%s)",
            getattr(result, "run_id", "?"),
            environment,
            exc_info=True,
        )
        return False


def persist_backtest_results_many(
    results: "list[Optional[BacktestResult]]",
    *,
    environment: str,
    experiment_id: Optional[str] = None,
) -> int:
    """批量入库多个 BacktestResult（复用单个 writer）。"""
    if not results:
        return 0
    saved = 0
    try:
        with _writer_scope(environment) as writer:
            repo = writer.backtest_repo
            for r in results:
                if r is None:
                    continue
                if experiment_id is not None:
                    try:
                        r.experiment_id = experiment_id
                    except Exception:
                        logger.debug("Cannot set experiment_id on BacktestResult")
                try:
                    repo.save_result(r)
                    saved += 1
                    logger.info("Backtest result persisted: run_id=%s", r.run_id)
                except Exception:
                    logger.warning(
                        "Failed to persist one backtest result run_id=%s",
                        getattr(r, "run_id", "?"),
                        exc_info=True,
                    )
    except Exception:
        logger.warning(
            "Failed to open writer for batch backtest persistence (env=%s)",
            environment,
            exc_info=True,
        )
    return saved


def add_persist_arguments(
    parser: Any, *, default_experiment: Optional[str] = None
) -> None:
    """给 argparse parser 添加 --persist / --experiment / --no-ensure-schema 三个通用选项。

    CLI 入口复用此 helper，确保 3 个 runner 的 flag 语义一致。
    """
    parser.add_argument(
        "--persist",
        action="store_true",
        help=(
            "把结果写入 DB（research_mining_runs / backtest_runs 等），"
            "不传则仅输出 stdout/JSON 供快速诊断（默认）"
        ),
    )
    parser.add_argument(
        "--experiment",
        default=default_experiment,
        metavar="EXPERIMENT_ID",
        help=(
            "关联实验 ID（ADR-007 Research→Backtest→Paper→Live 追踪）。"
            "仅在 --persist 时生效。留空则记录不绑定实验。"
        ),
    )
