"""回测 CLI 入口。

使用方式::

    python -m src.backtesting.cli run \\
        --symbol XAUUSD --timeframe M5 \\
        --start 2025-01-01 --end 2025-06-01 \\
        --strategies rsi_reversion,supertrend

    python -m src.backtesting.cli optimize \\
        --symbol XAUUSD --timeframe M5 \\
        --start 2025-01-01 --end 2025-06-01 \\
        --param "rsi_reversion__oversold=25,30,35" \\
        --param "rsi_reversion__overbought=70,75,80" \\
        --mode grid --sort sharpe_ratio
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _parse_param(param_str: str) -> tuple[str, List[Any]]:
    """解析参数定义字符串。

    格式: "key=val1,val2,val3"
    值自动转换为 float/int。
    """
    if "=" not in param_str:
        raise ValueError(f"Invalid param format: {param_str!r}. Expected 'key=val1,val2,...'")
    key, values_str = param_str.split("=", 1)
    values: List[Any] = []
    for v in values_str.split(","):
        v = v.strip()
        try:
            # 尝试 int
            values.append(int(v))
        except ValueError:
            try:
                values.append(float(v))
            except ValueError:
                values.append(v)
    return key.strip(), values


def _build_components(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    """构建回测所需的组件。"""
    from src.config.database import get_db_config
    from src.persistence.db import TimescaleWriter
    from src.persistence.repositories.market_repo import MarketRepository
    from src.signals.evaluation.regime import MarketRegimeDetector
    from src.signals.service import SignalModule
    from src.signals.strategies.adapters import UnifiedIndicatorSourceAdapter
    from src.indicators.engine.pipeline import get_global_pipeline
    from src.config.indicator_config import get_global_config_manager

    from .data_loader import HistoricalDataLoader

    # DB 连接
    db_config = get_db_config()
    writer = TimescaleWriter(
        host=db_config.host,
        port=db_config.port,
        dbname=db_config.dbname,
        user=db_config.user,
        password=db_config.password,
    )
    market_repo = MarketRepository(writer)
    data_loader = HistoricalDataLoader(market_repo)

    # 指标管线
    config_manager = get_global_config_manager()
    indicator_config = config_manager.get_config()
    pipeline = get_global_pipeline(indicator_config.pipeline)

    # 注册指标函数
    import importlib
    for ind_cfg in indicator_config.indicators:
        if not ind_cfg.enabled:
            continue
        parts = ind_cfg.func_path.rsplit(".", 1)
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])
        pipeline.register_indicator(
            name=ind_cfg.name,
            func=func,
            params=ind_cfg.params,
            dependencies=ind_cfg.dependencies or None,
        )

    # 信号模块
    class _NullIndicatorSource:
        def get_indicator(self, symbol: str, timeframe: str, name: str) -> Optional[Dict[str, Any]]:
            return None
        def get_all_indicators(self, symbol: str, timeframe: str) -> Dict[str, Dict[str, Any]]:
            return {}

    regime_detector = MarketRegimeDetector()
    signal_module = SignalModule(
        indicator_source=_NullIndicatorSource(),
        regime_detector=regime_detector,
        soft_regime_enabled=True,
    )

    # 注册复合策略
    from src.signals.strategies.registry import register_composite_strategies
    register_composite_strategies(signal_module)

    # 应用参数覆盖（如果有）
    if hasattr(args, "strategy_params") and args.strategy_params:
        from src.api.factories.signals import _apply_strategy_config_overrides

        class _FakeConfig:
            strategy_params: Dict[str, Any] = {}
            regime_affinity_overrides: Dict[str, Dict[str, float]] = {}

        fake_config = _FakeConfig()
        fake_config.strategy_params = args.strategy_params
        _apply_strategy_config_overrides(signal_module, fake_config)

    return {
        "data_loader": data_loader,
        "signal_module": signal_module,
        "pipeline": pipeline,
        "regime_detector": regime_detector,
        "writer": writer,
        "market_repo": market_repo,
    }


def cmd_run(args: argparse.Namespace) -> None:
    """执行单次回测。"""
    from .engine import BacktestEngine
    from .models import BacktestConfig
    from .report import format_summary

    strategies = args.strategies.split(",") if args.strategies else None
    config = BacktestConfig(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_time=datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc),
        strategies=strategies,
        initial_balance=args.balance,
        min_confidence=args.min_confidence,
        warmup_bars=args.warmup,
    )

    components = _build_components(args)
    engine = BacktestEngine(
        config=config,
        data_loader=components["data_loader"],
        signal_module=components["signal_module"],
        indicator_pipeline=components["pipeline"],
        regime_detector=components["regime_detector"],
    )

    result = engine.run()
    print(format_summary(result))

    if args.output:
        from .report import result_to_json

        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result_to_json(result))
        print(f"结果已保存到: {args.output}")


def cmd_optimize(args: argparse.Namespace) -> None:
    """执行参数优化。"""
    from .models import BacktestConfig, ParameterSpace
    from .optimizer import ParameterOptimizer, build_signal_module_with_overrides
    from .report import format_optimization_summary

    strategies = args.strategies.split(",") if args.strategies else None
    config = BacktestConfig(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_time=datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc),
        strategies=strategies,
        initial_balance=args.balance,
        min_confidence=args.min_confidence,
        warmup_bars=args.warmup,
    )

    # 解析参数空间
    param_dict: Dict[str, List[Any]] = {}
    for p in args.param:
        key, values = _parse_param(p)
        param_dict[key] = values

    param_space = ParameterSpace(
        strategy_params=param_dict,
        search_mode=args.mode,
        max_combinations=args.max_combos,
    )

    components = _build_components(args)
    base_module = components["signal_module"]

    def module_factory(params: Dict[str, Any]):  # type: ignore[no-untyped-def]
        return build_signal_module_with_overrides(base_module, params)

    optimizer = ParameterOptimizer(
        base_config=config,
        param_space=param_space,
        data_loader=components["data_loader"],
        indicator_pipeline=components["pipeline"],
        signal_module_factory=module_factory,
        regime_detector=components["regime_detector"],
        sort_metric=args.sort,
    )

    def progress(current: int, total: int, result: Any) -> None:
        print(
            f"  [{current}/{total}] "
            f"Sharpe={result.metrics.sharpe_ratio:.4f} "
            f"Win={result.metrics.win_rate * 100:.1f}% "
            f"PnL={result.metrics.total_pnl:+.2f}"
        )

    results = optimizer.run(progress_callback=progress)
    print(format_optimization_summary(results))


def main() -> None:
    """CLI 主入口。"""
    parser = argparse.ArgumentParser(
        description="MT5Services 回测工具",
        prog="python -m src.backtesting.cli",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="启用详细日志"
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # run 子命令
    run_parser = subparsers.add_parser("run", help="运行单次回测")
    _add_common_args(run_parser)
    run_parser.add_argument(
        "-o", "--output", type=str, help="输出 JSON 文件路径"
    )

    # optimize 子命令
    opt_parser = subparsers.add_parser("optimize", help="运行参数优化")
    _add_common_args(opt_parser)
    opt_parser.add_argument(
        "--param",
        action="append",
        required=True,
        help="参数定义 (格式: key=val1,val2,val3)，可多次指定",
    )
    opt_parser.add_argument(
        "--mode",
        choices=["grid", "random"],
        default="grid",
        help="搜索模式 (默认: grid)",
    )
    opt_parser.add_argument(
        "--max-combos",
        type=int,
        default=500,
        help="最大参数组合数 (默认: 500)",
    )
    opt_parser.add_argument(
        "--sort",
        type=str,
        default="sharpe_ratio",
        help="排序指标 (默认: sharpe_ratio)",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.command == "run":
        cmd_run(args)
    elif args.command == "optimize":
        cmd_optimize(args)
    else:
        parser.print_help()
        sys.exit(1)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """添加 run 和 optimize 共用的参数。"""
    parser.add_argument("--symbol", required=True, help="交易品种 (如 XAUUSD)")
    parser.add_argument("--timeframe", required=True, help="时间框架 (如 M5)")
    parser.add_argument("--start", required=True, help="起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="结束日期 (YYYY-MM-DD)")
    parser.add_argument(
        "--strategies", type=str, default=None, help="策略列表 (逗号分隔)"
    )
    parser.add_argument(
        "--balance", type=float, default=10000.0, help="初始资金 (默认: 10000)"
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.55,
        help="最低开仓置信度 (默认: 0.55)",
    )
    parser.add_argument(
        "--warmup", type=int, default=200, help="热身 bar 数量 (默认: 200)"
    )


if __name__ == "__main__":
    main()
