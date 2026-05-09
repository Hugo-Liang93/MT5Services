"""实盘上线前预检工具 — 验证配置、连接、风控参数一致性。

用法：
    python -m src.ops.cli.live_preflight --environment live
    python -m src.ops.cli.live_preflight --environment demo
    python -m src.ops.cli.live_preflight --environment demo --auto-launch-terminal
    python -m src.ops.cli.live_preflight --environment live --check-api    # 同时检查 API 是否在运行

功能：
    1. MT5 连接验证（终端、账户、品种可用性）
    2. 配置一致性检查（signal.ini ↔ backtest.ini 关键参数差异）
    3. 风控参数安全性审查
    4. TimescaleDB 连接与 schema 完整性
    5. 实盘 vs 回测参数差异对比表

设计原则：默认不启动服务、不执行交易，只做只读检查。
显式传入 --auto-launch-terminal 时，允许 MT5 Python API 用 mt5.local.ini 凭据
拉起 terminal 并建立 session；该模式仍不启动本服务、不下单。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

import logging
import warnings
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from src.ops.mt5_session_gate import (
    ensure_mt5_session_gate_or_raise,
    ensure_topology_group_mt5_session_gate_or_raise,
    probe_mt5_session_gate,
)


def _configure_cli_logging() -> None:
    warnings.filterwarnings("ignore")
    # 只抑制 DEBUG/INFO，保留 WARNING 以便检查告警
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("src").setLevel(logging.WARNING)


def _check_mt5_instance(
    instance_name: str | None = None,
    *,
    auto_launch_terminal: bool = False,
) -> List[Tuple[str, str, str]]:
    """检查单个实例的 MT5 会话门禁和账户。返回 [(check_name, status, detail)]。"""
    results: List[Tuple[str, str, str]] = []
    label_prefix = f"[{instance_name}] " if instance_name else ""

    try:
        from src.config.mt5 import load_mt5_settings

        mt5_cfg = load_mt5_settings(instance_name=instance_name)
        results.append(
            (f"{label_prefix}MT5 config loaded", "OK", f"server={mt5_cfg.mt5_server}")
        )
    except Exception as e:
        results.append((f"{label_prefix}MT5 config loaded", "FAIL", str(e)))
        return results

    try:
        from src.clients.base import MT5BaseClient, mt5

        client = MT5BaseClient(settings=mt5_cfg)
        state = client.inspect_session_state(
            require_terminal_process=not auto_launch_terminal,
            attempt_initialize=True,
            attempt_login=True,
            shutdown_after_probe=False,
        )

        path_status = "OK" if state.terminal_reachable else "FAIL"
        path_detail = (
            f"path={mt5_cfg.mt5_path}"
            if state.terminal_reachable
            else (f"{state.error_code or 'terminal_not_found'}: {state.error_message}")
        )
        results.append((f"{label_prefix}MT5 terminal path", path_status, path_detail))

        process_status = "OK" if state.terminal_process_ready else "FAIL"
        process_detail = (
            f"path={mt5_cfg.mt5_path}"
            if state.terminal_process_ready
            else f"{state.error_code or 'terminal_not_running'}: {state.error_message}"
        )
        results.append(
            (f"{label_prefix}MT5 terminal process", process_status, process_detail)
        )

        ipc_status = "OK" if state.ipc_ready else "FAIL"
        ipc_detail = (
            f"terminal={state.terminal_name or 'attached'}"
            if state.ipc_ready
            else f"{state.error_code or 'ipc_timeout'}: {state.error_message}"
        )
        results.append((f"{label_prefix}MT5 IPC", ipc_status, ipc_detail))

        auth_status = "OK" if state.authorized else "FAIL"
        auth_detail = (
            f"login={state.login} server={state.server}"
            if state.authorized
            else f"{state.error_code or 'login_failed'}: {state.error_message}"
        )
        results.append((f"{label_prefix}MT5 authorization", auth_status, auth_detail))

        account_status = "OK" if state.account_match else "FAIL"
        account_detail = (
            f"login={state.login} server={state.server}"
            if state.account_match
            else f"{state.error_code or 'account_mismatch'}: {state.error_message}"
        )
        results.append(
            (f"{label_prefix}MT5 account match", account_status, account_detail)
        )

        session_status = "OK" if state.session_ready else "FAIL"
        session_detail = (
            f"ready login={state.login} server={state.server}"
            if state.session_ready
            else f"{state.error_code or 'session_not_ready'}: {state.error_message}"
        )
        results.append(
            (f"{label_prefix}MT5 session gate", session_status, session_detail)
        )

        if state.interactive_login_required:
            results.append(
                (
                    f"{label_prefix}MT5 interactive login",
                    "FAIL",
                    "interactive_login_required: terminal needs manual unlock/login",
                )
            )

        if state.session_ready and mt5 is not None:
            acct = mt5.account_info()
            if acct:
                account_type = "DEMO" if acct.trade_mode == 0 else "LIVE"
                results.append(
                    (
                        f"{label_prefix}MT5 account",
                        "OK",
                        f"login={acct.login} type={account_type} "
                        f"balance={acct.balance:.2f} equity={acct.equity:.2f} "
                        f"leverage=1:{acct.leverage} currency={acct.currency}",
                    )
                )
                if account_type == "DEMO":
                    results.append(
                        (
                            f"{label_prefix}Account type",
                            "WARN",
                            "Still on DEMO — switch to LIVE for real trading",
                        )
                    )
            else:
                results.append(
                    (
                        f"{label_prefix}MT5 account",
                        "FAIL",
                        "account_info() returned None",
                    )
                )

            # SSOT 是 config/app.ini trading.symbols；硬编码 XAUUSD 在多品种或
            # 改名场景会把"目标 symbol 不可见"误报成"XAUUSD 不可见"
            try:
                from src.config.centralized import get_trading_config

                trading_cfg = get_trading_config()
                target_symbols = list(trading_cfg.symbols) or [
                    trading_cfg.default_symbol
                ]
            except Exception:
                target_symbols = ["XAUUSD"]

            for symbol_name in target_symbols:
                symbol_info = mt5.symbol_info(symbol_name)
                if symbol_info:
                    results.append(
                        (
                            f"{label_prefix}{symbol_name} symbol",
                            "OK",
                            f"spread={symbol_info.spread} "
                            f"point={symbol_info.point} "
                            f"volume_min={symbol_info.volume_min} "
                            f"volume_max={symbol_info.volume_max}",
                        )
                    )
                else:
                    results.append(
                        (
                            f"{label_prefix}{symbol_name} symbol",
                            "FAIL",
                            "Symbol not found or not visible",
                        )
                    )

            mt5.shutdown()
    except ImportError:
        results.append(
            (f"{label_prefix}MT5 terminal", "SKIP", "MetaTrader5 package not installed")
        )
    except Exception as e:
        results.append((f"{label_prefix}MT5 terminal", "FAIL", str(e)))

    return results


def _check_mt5(
    environment: str | None = None,
    *,
    auto_launch_terminal: bool = False,
) -> List[Tuple[str, str, str]]:
    try:
        from src.config.topology import load_topology_group

        group_name = str(environment or "").strip()
        if group_name:
            group = load_topology_group(group_name)
            results: List[Tuple[str, str, str]] = []
            for instance_name in [group.main, *group.workers]:
                results.extend(
                    _check_mt5_instance(
                        instance_name,
                        auto_launch_terminal=auto_launch_terminal,
                    )
                )
            return results
    except Exception:
        pass
    return _check_mt5_instance(auto_launch_terminal=auto_launch_terminal)


def _check_database() -> List[Tuple[str, str, str]]:
    """检查 TimescaleDB 连接。"""
    results: List[Tuple[str, str, str]] = []

    try:
        from src.config.database import load_db_settings
        from src.persistence.db import TimescaleWriter

        db_config = load_db_settings()
        results.append(
            ("DB config loaded", "OK", f"host={db_config.pg_host}:{db_config.pg_port}")
        )

        writer = TimescaleWriter(settings=db_config, min_conn=1, max_conn=2)
        # 简单查询验证连接
        try:
            conn = writer._pool.getconn()
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'"
            )
            table_count = cur.fetchone()[0]
            cur.close()
            writer._pool.putconn(conn)
            results.append(
                ("DB connection", "OK", f"{table_count} tables in public schema")
            )
        except Exception as e:
            results.append(("DB connection", "FAIL", str(e)))
    except Exception as e:
        results.append(("DB config", "FAIL", str(e)))

    return results


def _check_config_consistency() -> (
    Tuple[List[Tuple[str, str, str]], List[Dict[str, Any]]]
):
    """检查 signal.ini 与 backtest.ini 的关键参数一致性。

    返回 (check_results, diff_table)。
    """
    results: List[Tuple[str, str, str]] = []
    diff_table: List[Dict[str, Any]] = []

    try:
        from src.backtesting.config import get_backtest_defaults
        from src.config.centralized import get_risk_config
        from src.config.signal import get_signal_config

        signal_cfg = get_signal_config()
        risk_cfg = get_risk_config()
        bt_defaults = get_backtest_defaults()

        # 关键对比项：实盘 signal.ini 值 vs 回测 backtest.ini 值
        comparisons = [
            # 真实 live 阈值是 auto_trade_min_confidence；旧字段已从 SignalConfig
            # 移除（参 codebase-review §0o），旧 getattr 走默认 0.55 让差异表变成伪事实
            (
                "min_confidence",
                getattr(signal_cfg, "auto_trade_min_confidence", 0.55),
                bt_defaults.get("min_confidence", 0.55),
            ),
            (
                "trailing_tp_enabled",
                getattr(signal_cfg, "trailing_tp_enabled", True),
                bt_defaults.get("trailing_tp_enabled", True),
            ),
            (
                "trailing_tp_activation_atr",
                getattr(signal_cfg, "trailing_tp_activation_atr", 1.2),
                bt_defaults.get("trailing_tp_activation_atr", 1.5),
            ),
            (
                "trailing_tp_trail_atr",
                getattr(signal_cfg, "trailing_tp_trail_atr", 0.6),
                bt_defaults.get("trailing_tp_trail_atr", 0.8),
            ),
            ("commission_per_lot", 7.0, bt_defaults.get("commission_per_lot", 0.0)),
            ("slippage_points", 15.0, bt_defaults.get("slippage_points", 0.0)),
            (
                "max_positions",
                getattr(risk_cfg, "max_positions_per_symbol", 3),
                bt_defaults.get("max_positions", 3),
            ),
            (
                "daily_loss_limit_pct",
                getattr(risk_cfg, "daily_loss_limit_pct", None),
                bt_defaults.get("daily_loss_limit_pct", None),
            ),
        ]

        for param, live_val, bt_val in comparisons:
            match = live_val == bt_val
            diff_table.append(
                {
                    "param": param,
                    "live": live_val,
                    "backtest": bt_val,
                    "match": match,
                }
            )
            if not match:
                results.append(
                    (
                        f"Config: {param}",
                        "DIFF",
                        f"live={live_val} vs backtest={bt_val}",
                    )
                )

        # 关键安全检查
        # 1. commission 是否为 0
        if bt_defaults.get("commission_per_lot", 0) == 0:
            results.append(
                (
                    "Backtest commission",
                    "WARN",
                    "commission_per_lot=0 in backtest — results will be overly optimistic",
                )
            )

        # 2. slippage 是否为 0
        if bt_defaults.get("slippage_points", 0) == 0:
            results.append(
                (
                    "Backtest slippage",
                    "WARN",
                    "slippage_points=0 in backtest — no slippage simulation",
                )
            )

        # 3. daily_loss_limit 是否配置
        if getattr(risk_cfg, "daily_loss_limit_pct", None) is None:
            results.append(
                (
                    "Risk: daily_loss_limit",
                    "WARN",
                    "No daily loss limit configured in risk.ini",
                )
            )

        results.append(
            ("Config consistency check", "OK", f"{len(diff_table)} params compared")
        )

    except Exception as e:
        results.append(("Config loading", "FAIL", str(e)))

    return results, diff_table


def _check_effective_strategy_routing(environment: str) -> List[Tuple[str, str, str]]:
    """审计 auto-trade 下可执行策略是否有正式账户路由。"""
    results: List[Tuple[str, str, str]] = []
    env = str(environment or "").strip().lower()

    try:
        from src.config.signal import get_signal_config
        from src.signals.strategies.catalog import build_named_strategy_catalog

        signal_cfg = get_signal_config()
        catalog = build_named_strategy_catalog()
        known_strategies = {str(name).strip() for name in catalog if str(name).strip()}
        deployments = dict(getattr(signal_cfg, "strategy_deployments", {}) or {})
        account_bindings = _normalize_account_bindings(
            getattr(signal_cfg, "account_bindings", {}) or {}
        )
        account_aliases = _environment_account_aliases(env)
        if account_aliases:
            account_bindings = {
                alias: strategies
                for alias, strategies in account_bindings.items()
                if alias in account_aliases
            }

        if not bool(getattr(signal_cfg, "auto_trade_enabled", False)):
            results.append(
                (
                    "Effective strategy routing",
                    "OK",
                    "auto_trade_enabled=false; execution routing not required",
                )
            )
            return results

        bound_pairs = [
            (account_alias, strategy_name)
            for account_alias, strategies in account_bindings.items()
            for strategy_name in strategies
        ]
        bound_strategies = {strategy_name for _, strategy_name in bound_pairs}

        unknown_bound = sorted(
            strategy_name
            for strategy_name in bound_strategies
            if strategy_name not in known_strategies
        )
        if unknown_bound:
            results.append(
                (
                    "Effective strategy routing",
                    "FAIL",
                    "account_bindings reference unregistered strategies: "
                    + ", ".join(unknown_bound),
                )
            )

        missing_deployments = sorted(
            strategy_name
            for strategy_name in bound_strategies
            if strategy_name in known_strategies and strategy_name not in deployments
        )
        if missing_deployments:
            results.append(
                (
                    "Effective strategy routing",
                    "FAIL",
                    "account_bindings reference strategies without deployment "
                    "contracts: " + ", ".join(missing_deployments),
                )
            )

        executable_strategies: list[str] = []
        non_executable_bound: list[str] = []
        for strategy_name, deployment in deployments.items():
            if strategy_name not in known_strategies:
                results.append(
                    (
                        "Effective strategy routing",
                        "FAIL",
                        "strategy_deployments reference unregistered strategy: "
                        f"{strategy_name}",
                    )
                )
                continue
            if deployment.is_executable_in(env):
                executable_strategies.append(strategy_name)
            elif strategy_name in bound_strategies:
                non_executable_bound.append(strategy_name)

        if non_executable_bound:
            results.append(
                (
                    "Effective strategy routing",
                    "FAIL",
                    f"bound strategies not executable in {env}: "
                    + ", ".join(sorted(non_executable_bound)),
                )
            )

        unrouted = sorted(
            strategy_name
            for strategy_name in executable_strategies
            if strategy_name not in bound_strategies
        )
        if unrouted:
            results.append(
                (
                    "Effective strategy routing",
                    "FAIL",
                    "auto_trade_enabled=true but executable strategies are not "
                    "bound to any account: " + ", ".join(unrouted),
                )
            )

        if results:
            return results

        if not executable_strategies:
            results.append(
                (
                    "Effective strategy routing",
                    "OK",
                    f"auto_trade_enabled=true; no strategy executable in {env}",
                )
            )
            return results

        routed = ", ".join(
            f"{account_alias}:{strategy_name}"
            for account_alias, strategy_name in bound_pairs
            if strategy_name in executable_strategies
        )
        results.append(
            (
                "Effective strategy routing",
                "OK",
                f"auto_trade_enabled=true; routed={routed}",
            )
        )
    except Exception as e:
        results.append(("Effective strategy routing", "FAIL", str(e)))

    return results


def _normalize_account_bindings(
    raw_bindings: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for raw_alias, raw_strategies in raw_bindings.items():
        account_alias = str(raw_alias).strip()
        if not account_alias:
            continue
        strategies: list[str] = []
        for raw_strategy in raw_strategies:
            strategy_name = str(raw_strategy).strip()
            if strategy_name and strategy_name not in strategies:
                strategies.append(strategy_name)
        if strategies:
            normalized[account_alias] = strategies
    return normalized


def _environment_account_aliases(environment: str) -> set[str]:
    try:
        from src.config.topology import load_topology_group

        group = load_topology_group(environment)
    except Exception:
        return set()

    aliases: set[str] = set()
    for raw_instance in tuple(
        getattr(group, "instances", (group.main, *group.workers))
    ):
        instance_name = str(raw_instance).strip()
        if not instance_name:
            continue
        aliases.add(instance_name)
        aliases.add(instance_name.replace("-", "_"))
    return aliases


def _check_risk_safety() -> List[Tuple[str, str, str]]:
    """风控参数安全性审查。"""
    results: List[Tuple[str, str, str]] = []

    try:
        from src.config.centralized import get_risk_config

        risk_cfg = get_risk_config()

        # 手数限制
        max_vol = getattr(risk_cfg, "max_volume_per_order", None)
        if max_vol is None:
            results.append(
                (
                    "Risk: max_volume_per_order",
                    "WARN",
                    "Not set — no per-order volume limit",
                )
            )
        elif max_vol > 0.1:
            results.append(
                (
                    "Risk: max_volume_per_order",
                    "WARN",
                    f"{max_vol} lots — consider 0.01-0.05 for initial live",
                )
            )
        else:
            results.append(("Risk: max_volume_per_order", "OK", f"{max_vol} lots"))

        # 每日亏损限制
        daily_limit = getattr(risk_cfg, "daily_loss_limit_pct", None)
        if daily_limit is None:
            results.append(
                (
                    "Risk: daily_loss_limit_pct",
                    "WARN",
                    "Not set — no daily loss circuit breaker",
                )
            )
        elif daily_limit > 5.0:
            results.append(
                (
                    "Risk: daily_loss_limit_pct",
                    "WARN",
                    f"{daily_limit}% — consider <=3% for safety",
                )
            )
        else:
            results.append(("Risk: daily_loss_limit_pct", "OK", f"{daily_limit}%"))

        # 每日交易次数限制
        max_trades = getattr(risk_cfg, "max_trades_per_day", None)
        if max_trades is None:
            results.append(
                ("Risk: max_trades_per_day", "WARN", "Not set — unlimited daily trades")
            )
        else:
            results.append(("Risk: max_trades_per_day", "OK", f"{max_trades}"))

    except Exception as e:
        results.append(("Risk config", "FAIL", str(e)))

    return results


def _load_instance_risk_config(instance_name: str):
    from src.config.models import RiskConfig
    from src.config.risk import normalize_risk_config_payload
    from src.config.utils import load_config_with_base

    _, parser = load_config_with_base("risk.ini", instance_name=instance_name)
    if parser is None or not parser.has_section("risk"):
        return RiskConfig()
    preflight_policy = (
        dict(parser.items("preflight_risk_policy"))
        if parser.has_section("preflight_risk_policy")
        else {}
    )
    return RiskConfig.model_validate(
        normalize_risk_config_payload(dict(parser.items("risk")), preflight_policy)
    )


def _check_instance_risk_safety(environment: str) -> List[Tuple[str, str, str]]:
    """按 topology 实例审计 local 覆盖后的有效风控。"""
    env = str(environment or "").strip().lower()

    try:
        from src.config.topology import load_topology_group

        group = load_topology_group(env)
        instance_names = tuple(
            getattr(group, "instances", (group.main, *group.workers))
        )
    except Exception as e:
        return [("Instance risk topology", "FAIL", str(e))]

    results: List[Tuple[str, str, str]] = []
    for instance_name in instance_names:
        try:
            risk_cfg = _load_instance_risk_config(str(instance_name))
        except Exception as e:
            results.append((f"[{instance_name}] Risk config", "FAIL", str(e)))
            continue

        results.extend(_evaluate_instance_risk(str(instance_name), risk_cfg, env))

    return results


def _evaluate_instance_risk(
    instance_name: str,
    risk_cfg: Any,
    environment: str,
) -> List[Tuple[str, str, str]]:
    label = f"[{instance_name}] "
    live_env = str(environment or "").strip().lower() == "live"
    results: List[Tuple[str, str, str]] = []
    from src.config.models import PreflightRiskPolicy

    policy = getattr(risk_cfg, "preflight_policy", None) or PreflightRiskPolicy()

    data_policy = (
        str(getattr(risk_cfg, "data_unavailable_policy", "") or "").strip().lower()
    )
    if not data_policy:
        status = "FAIL" if live_env else "WARN"
        detail = "not set; live hard-risk fact sources must fail closed"
    elif live_env and data_policy != "fail_closed":
        status = "FAIL"
        detail = f"{data_policy}; live hard-risk fact sources must use fail_closed"
    else:
        status = "OK"
        detail = data_policy
    results.append((f"{label}Risk: data_unavailable_policy", status, detail))

    max_positions = getattr(risk_cfg, "max_positions_per_symbol", None)
    if max_positions is None:
        status = "FAIL" if live_env else "WARN"
        detail = "not set; instance can accumulate unlimited same-symbol positions"
    elif live_env and int(max_positions) > int(policy.live_max_positions_per_symbol):
        status = "FAIL"
        detail = (
            f"{max_positions}; live limit must be "
            f"<={policy.live_max_positions_per_symbol}"
        )
    else:
        status = "OK"
        detail = f"{max_positions}"
    results.append((f"{label}Risk: max_positions_per_symbol", status, detail))

    max_order_volume = getattr(risk_cfg, "max_volume_per_order", None)
    if max_order_volume is None:
        status = "FAIL" if live_env else "WARN"
        detail = "not set; per-order lot cap is required"
    elif live_env and float(max_order_volume) > float(policy.live_max_volume_per_order):
        status = "FAIL"
        detail = (
            f"{max_order_volume} lots; live cap must be "
            f"<={policy.live_max_volume_per_order}"
        )
    else:
        status = "OK"
        detail = f"{max_order_volume} lots"
    results.append((f"{label}Risk: max_volume_per_order", status, detail))

    max_symbol_volume = getattr(risk_cfg, "max_volume_per_symbol", None)
    if max_symbol_volume is None:
        status = "FAIL" if live_env else "WARN"
        detail = "not set; per-symbol aggregate lot cap is required"
    elif live_env and float(max_symbol_volume) > float(
        policy.live_max_volume_per_symbol
    ):
        status = "FAIL"
        detail = (
            f"{max_symbol_volume} lots; live aggregate cap must be "
            f"<={policy.live_max_volume_per_symbol}"
        )
    else:
        status = "OK"
        detail = f"{max_symbol_volume} lots"
    results.append((f"{label}Risk: max_volume_per_symbol", status, detail))

    daily_limit = getattr(risk_cfg, "daily_loss_limit_pct", None)
    if daily_limit is None:
        status = "FAIL" if live_env else "WARN"
        detail = "not set; daily loss circuit breaker is required"
    elif live_env and float(daily_limit) > float(policy.live_max_daily_loss_limit_pct):
        status = "FAIL"
        detail = (
            f"{daily_limit}%; live cap must be "
            f"<={policy.live_max_daily_loss_limit_pct}%"
        )
    else:
        status = "OK"
        detail = f"{daily_limit}%"
    results.append((f"{label}Risk: daily_loss_limit_pct", status, detail))

    max_trades = getattr(risk_cfg, "max_trades_per_day", None)
    if max_trades is None:
        status = "WARN"
        detail = "not set; daily trade count is unlimited"
    else:
        status = "OK"
        detail = f"{max_trades}"
    results.append((f"{label}Risk: max_trades_per_day", status, detail))

    return results


def _check_api(api_base: str = "http://localhost:8808") -> List[Tuple[str, str, str]]:
    """检查 API 服务状态。

    路由：根 ``/health`` 返回 ApiResponse[dict]，data 含 mode/market/trading/runtime。
    （历史曾 probe ``/v1/health`` 但该路径已不存在；当前 health 路由分两层：
    业务级 ``/health`` 在 root，K8s 探针 ``/v1/monitoring/health/{live,ready}``。
    preflight 想要业务 health 故取 root。）
    """
    results: List[Tuple[str, str, str]] = []
    import requests

    try:
        r = requests.get(f"{api_base}/health", timeout=5)
        if r.status_code != 200:
            results.append(("API health", "FAIL", f"HTTP {r.status_code}"))
            return results

        payload = r.json()
        if not payload.get("success", False):
            err = payload.get("error", {}) or {}
            err_code = err.get("code") if isinstance(err, dict) else None
            detail = f"code={err_code}" if err_code else "success=False"
            results.append(("API health", "FAIL", detail))
            return results

        data = payload.get("data") or {}
        mode = data.get("mode", "unknown")
        market_connected = bool((data.get("market") or {}).get("connected", False))
        trading_running = bool((data.get("trading") or {}).get("running", False))
        detail = (
            f"mode={mode} "
            f"market={'connected' if market_connected else 'disconnected'} "
            f"trading={'running' if trading_running else 'stopped'}"
        )
        results.append(("API health", "OK", detail))
    except requests.ConnectionError:
        results.append(
            (
                "API health",
                "SKIP",
                "Service not running (expected if checking before start)",
            )
        )
    except (requests.Timeout, requests.RequestException, ValueError) as e:
        # 异常分层：网络/JSON 解析降级；coding error（AttributeError/TypeError）透传
        results.append(("API health", "FAIL", str(e)))

    return results


def _render_results(
    all_checks: List[Tuple[str, str, str]],
    diff_table: List[Dict[str, Any]],
) -> str:
    """渲染预检结果。"""
    lines = [
        f"\n{'='*70}",
        " LIVE TRADING PRE-FLIGHT CHECK",
        f"{'='*70}",
    ]

    # 分类统计
    ok_count = sum(1 for _, s, _ in all_checks if s == "OK")
    warn_count = sum(1 for _, s, _ in all_checks if s == "WARN")
    fail_count = sum(1 for _, s, _ in all_checks if s == "FAIL")
    diff_count = sum(1 for _, s, _ in all_checks if s == "DIFF")

    lines.append(
        f"\n  Summary: {ok_count} OK / {warn_count} WARN / {fail_count} FAIL / {diff_count} DIFF\n"
    )

    # 逐项结果
    for check_name, status, detail in all_checks:
        icon = {
            "OK": "[+]",
            "WARN": "[!]",
            "FAIL": "[X]",
            "DIFF": "[~]",
            "SKIP": "[-]",
        }.get(status, "[?]")
        lines.append(f"  {icon} {check_name:<35} {detail}")

    # 实盘 vs 回测参数差异表
    if diff_table:
        lines.append(f"\n--- Live vs Backtest Parameter Comparison ---")
        lines.append(f"  {'Parameter':<30} {'Live':>12} {'Backtest':>12} {'Match':>7}")
        lines.append("  " + "-" * 65)
        for row in diff_table:
            match_str = "YES" if row["match"] else "*** NO"
            lines.append(
                f"  {row['param']:<30} {str(row['live']):>12} {str(row['backtest']):>12} {match_str:>7}"
            )

    # 总结建议
    lines.append(f"\n--- Verdict ---")
    if fail_count > 0:
        lines.append(
            f"  [X] BLOCKED: {fail_count} critical failures must be resolved before going live."
        )
    elif warn_count > 0:
        lines.append(f"  [!] PROCEED WITH CAUTION: {warn_count} warnings to review.")
        lines.append(f"  Recommended: fix warnings before P3 live trading.")
    else:
        lines.append(f"  [+] ALL CLEAR: System ready for live trading.")

    if diff_count > 0:
        lines.append(
            f"  [~] {diff_count} parameter differences between live and backtest configs."
        )
        lines.append(
            f"  These differences mean backtest results may not fully predict live performance."
        )
        lines.append(
            f"  Align backtest.ini to match live settings for accurate prediction."
        )

    return "\n".join(lines)


def main() -> None:
    from src.config.instance_context import set_current_environment

    _configure_cli_logging()
    parser = argparse.ArgumentParser(description="Live trading pre-flight check")
    parser.add_argument(
        "--environment",
        choices=["live", "demo"],
        required=True,
        help="显式指定预检环境",
    )
    parser.add_argument(
        "--check-api", action="store_true", help="Also check API service"
    )
    parser.add_argument(
        "--auto-launch-terminal",
        action="store_true",
        help=(
            "Allow MT5 Python API to launch terminal and login using mt5.local.ini "
            "credentials during the MT5 session check"
        ),
    )
    args = parser.parse_args()
    set_current_environment(args.environment)

    all_checks: List[Tuple[str, str, str]] = []

    sys.stderr.write("Checking MT5 connection...\n")
    all_checks.extend(
        _check_mt5(
            args.environment,
            auto_launch_terminal=args.auto_launch_terminal,
        )
    )

    sys.stderr.write("Checking database...\n")
    all_checks.extend(_check_database())

    sys.stderr.write("Checking config consistency...\n")
    config_checks, diff_table = _check_config_consistency()
    all_checks.extend(config_checks)

    sys.stderr.write("Checking effective strategy routing...\n")
    all_checks.extend(_check_effective_strategy_routing(args.environment))

    sys.stderr.write("Checking risk safety...\n")
    all_checks.extend(_check_risk_safety())
    all_checks.extend(_check_instance_risk_safety(args.environment))

    if args.check_api:
        sys.stderr.write("Checking API service...\n")
        all_checks.extend(_check_api())

    output = _render_results(all_checks, diff_table)
    print(output)
    if any(status == "FAIL" for _, status, _ in all_checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
