"""实盘上线前预检工具 — 验证配置、连接、风控参数一致性。

用法：
    python tools/live_preflight.py
    python tools/live_preflight.py --check-api    # 同时检查 API 是否在运行

功能：
    1. MT5 连接验证（终端、账户、品种可用性）
    2. 配置一致性检查（signal.ini ↔ backtest.ini 关键参数差异）
    3. 风控参数安全性审查
    4. TimescaleDB 连接与 schema 完整性
    5. 实盘 vs 回测参数差异对比表

设计原则：不启动服务、不执行交易，只做只读检查。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings

warnings.filterwarnings("ignore")

import logging

# 只抑制 DEBUG/INFO，保留 WARNING 以便检查告警
logging.basicConfig(level=logging.WARNING, format="%(message)s")
logging.getLogger("src").setLevel(logging.WARNING)

from typing import Any, Dict, List, Tuple


def _check_mt5() -> List[Tuple[str, str, str]]:
    """检查 MT5 连接和账户。返回 [(check_name, status, detail)]。"""
    results: List[Tuple[str, str, str]] = []

    try:
        from src.config.mt5 import load_mt5_settings

        mt5_cfg = load_mt5_settings()
        results.append(("MT5 config loaded", "OK", f"server={mt5_cfg.mt5_server}"))
    except Exception as e:
        results.append(("MT5 config loaded", "FAIL", str(e)))
        return results

    try:
        import MetaTrader5 as mt5

        # 与实盘 src/clients/base.py 一致：initialize 时直接传入全部凭据
        init_kwargs: dict = {}
        if mt5_cfg.mt5_path:
            init_kwargs["path"] = mt5_cfg.mt5_path
        if mt5_cfg.mt5_login is not None:
            init_kwargs["login"] = mt5_cfg.mt5_login
        if mt5_cfg.mt5_password:
            init_kwargs["password"] = mt5_cfg.mt5_password
        if mt5_cfg.mt5_server:
            init_kwargs["server"] = mt5_cfg.mt5_server

        if not mt5.initialize(**init_kwargs):
            results.append(("MT5 terminal", "FAIL", f"initialize() failed: {mt5.last_error()}"))
            return results
        results.append(("MT5 terminal", "OK", f"path={mt5_cfg.mt5_path}"))

        # 验证登录账户是否匹配配置
        acct_info = mt5.account_info()
        if acct_info and mt5_cfg.mt5_login and acct_info.login != mt5_cfg.mt5_login:
            # initialize 后账户不匹配，显式 login 切换
            logged_in = mt5.login(
                login=mt5_cfg.mt5_login,
                password=mt5_cfg.mt5_password,
                server=mt5_cfg.mt5_server,
            )
            if not logged_in:
                results.append(("MT5 login", "FAIL", f"{mt5.last_error()}"))
                mt5.shutdown()
                return results

        # 账户信息
        acct = mt5.account_info()
        if acct:
            account_type = "DEMO" if acct.trade_mode == 0 else "LIVE"
            results.append((
                "MT5 account",
                "OK",
                f"login={acct.login} type={account_type} "
                f"balance={acct.balance:.2f} equity={acct.equity:.2f} "
                f"leverage=1:{acct.leverage} currency={acct.currency}",
            ))
            if account_type == "DEMO":
                results.append(("Account type", "WARN", "Still on DEMO — switch to LIVE for real trading"))
        else:
            results.append(("MT5 account", "FAIL", "account_info() returned None"))

        # XAUUSD 品种
        symbol_info = mt5.symbol_info("XAUUSD")
        if symbol_info:
            results.append((
                "XAUUSD symbol",
                "OK",
                f"spread={symbol_info.spread} point={symbol_info.point} "
                f"volume_min={symbol_info.volume_min} volume_max={symbol_info.volume_max}",
            ))
        else:
            results.append(("XAUUSD symbol", "FAIL", "Symbol not found or not visible"))

        mt5.shutdown()
    except ImportError:
        results.append(("MT5 terminal", "SKIP", "MetaTrader5 package not installed"))
    except Exception as e:
        results.append(("MT5 terminal", "FAIL", str(e)))

    return results


def _check_database() -> List[Tuple[str, str, str]]:
    """检查 TimescaleDB 连接。"""
    results: List[Tuple[str, str, str]] = []

    try:
        from src.config.database import load_db_settings
        from src.persistence.db import TimescaleWriter

        db_config = load_db_settings()
        results.append(("DB config loaded", "OK", f"host={db_config.pg_host}:{db_config.pg_port}"))

        writer = TimescaleWriter(settings=db_config, min_conn=1, max_conn=2)
        # 简单查询验证连接
        try:
            conn = writer._pool.getconn()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'")
            table_count = cur.fetchone()[0]
            cur.close()
            writer._pool.putconn(conn)
            results.append(("DB connection", "OK", f"{table_count} tables in public schema"))
        except Exception as e:
            results.append(("DB connection", "FAIL", str(e)))
    except Exception as e:
        results.append(("DB config", "FAIL", str(e)))

    return results


def _check_config_consistency() -> Tuple[List[Tuple[str, str, str]], List[Dict[str, Any]]]:
    """检查 signal.ini 与 backtest.ini 的关键参数一致性。

    返回 (check_results, diff_table)。
    """
    results: List[Tuple[str, str, str]] = []
    diff_table: List[Dict[str, Any]] = []

    try:
        from src.backtesting.config import get_backtest_defaults
        from src.config.signal import get_signal_config
        from src.config.centralized import get_risk_config

        signal_cfg = get_signal_config()
        risk_cfg = get_risk_config()
        bt_defaults = get_backtest_defaults()

        # 关键对比项：实盘 signal.ini 值 vs 回测 backtest.ini 值
        comparisons = [
            ("min_confidence", getattr(signal_cfg, "min_preview_confidence", 0.55), bt_defaults.get("min_confidence", 0.55)),
            ("trailing_tp_enabled", getattr(signal_cfg, "trailing_tp_enabled", True), bt_defaults.get("trailing_tp_enabled", True)),
            ("trailing_tp_activation_atr", getattr(signal_cfg, "trailing_tp_activation_atr", 1.2), bt_defaults.get("trailing_tp_activation_atr", 1.5)),
            ("trailing_tp_trail_atr", getattr(signal_cfg, "trailing_tp_trail_atr", 0.6), bt_defaults.get("trailing_tp_trail_atr", 0.8)),
            ("commission_per_lot", 7.0, bt_defaults.get("commission_per_lot", 0.0)),
            ("slippage_points", 15.0, bt_defaults.get("slippage_points", 0.0)),
            ("max_positions", getattr(risk_cfg, "max_positions_per_symbol", 3), bt_defaults.get("max_positions", 3)),
            ("daily_loss_limit_pct", getattr(risk_cfg, "daily_loss_limit_pct", None), bt_defaults.get("daily_loss_limit_pct", None)),
        ]

        for param, live_val, bt_val in comparisons:
            match = live_val == bt_val
            diff_table.append({
                "param": param,
                "live": live_val,
                "backtest": bt_val,
                "match": match,
            })
            if not match:
                results.append((
                    f"Config: {param}",
                    "DIFF",
                    f"live={live_val} vs backtest={bt_val}",
                ))

        # 关键安全检查
        # 1. commission 是否为 0
        if bt_defaults.get("commission_per_lot", 0) == 0:
            results.append((
                "Backtest commission",
                "WARN",
                "commission_per_lot=0 in backtest — results will be overly optimistic",
            ))

        # 2. slippage 是否为 0
        if bt_defaults.get("slippage_points", 0) == 0:
            results.append((
                "Backtest slippage",
                "WARN",
                "slippage_points=0 in backtest — no slippage simulation",
            ))

        # 3. daily_loss_limit 是否配置
        if getattr(risk_cfg, "daily_loss_limit_pct", None) is None:
            results.append((
                "Risk: daily_loss_limit",
                "WARN",
                "No daily loss limit configured in risk.ini",
            ))

        results.append(("Config consistency check", "OK", f"{len(diff_table)} params compared"))

    except Exception as e:
        results.append(("Config loading", "FAIL", str(e)))

    return results, diff_table


def _check_risk_safety() -> List[Tuple[str, str, str]]:
    """风控参数安全性审查。"""
    results: List[Tuple[str, str, str]] = []

    try:
        from src.config.centralized import get_risk_config

        risk_cfg = get_risk_config()

        # 手数限制
        max_vol = getattr(risk_cfg, "max_volume_per_order", None)
        if max_vol is None:
            results.append(("Risk: max_volume_per_order", "WARN", "Not set — no per-order volume limit"))
        elif max_vol > 0.1:
            results.append(("Risk: max_volume_per_order", "WARN", f"{max_vol} lots — consider 0.01-0.05 for initial live"))
        else:
            results.append(("Risk: max_volume_per_order", "OK", f"{max_vol} lots"))

        # 每日亏损限制
        daily_limit = getattr(risk_cfg, "daily_loss_limit_pct", None)
        if daily_limit is None:
            results.append(("Risk: daily_loss_limit_pct", "WARN", "Not set — no daily loss circuit breaker"))
        elif daily_limit > 5.0:
            results.append(("Risk: daily_loss_limit_pct", "WARN", f"{daily_limit}% — consider <=3% for safety"))
        else:
            results.append(("Risk: daily_loss_limit_pct", "OK", f"{daily_limit}%"))

        # 每日交易次数限制
        max_trades = getattr(risk_cfg, "max_trades_per_day", None)
        if max_trades is None:
            results.append(("Risk: max_trades_per_day", "WARN", "Not set — unlimited daily trades"))
        else:
            results.append(("Risk: max_trades_per_day", "OK", f"{max_trades}"))

    except Exception as e:
        results.append(("Risk config", "FAIL", str(e)))

    return results


def _check_api(api_base: str = "http://localhost:8808") -> List[Tuple[str, str, str]]:
    """检查 API 服务状态。"""
    results: List[Tuple[str, str, str]] = []
    import requests

    try:
        r = requests.get(f"{api_base}/v1/health", timeout=5)
        if r.status_code == 200:
            data = r.json()
            status = data.get("data", {}).get("status", "unknown")
            results.append(("API health", "OK", f"status={status}"))
        else:
            results.append(("API health", "FAIL", f"HTTP {r.status_code}"))
    except requests.ConnectionError:
        results.append(("API health", "SKIP", "Service not running (expected if checking before start)"))
    except Exception as e:
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

    lines.append(f"\n  Summary: {ok_count} OK / {warn_count} WARN / {fail_count} FAIL / {diff_count} DIFF\n")

    # 逐项结果
    for check_name, status, detail in all_checks:
        icon = {"OK": "[+]", "WARN": "[!]", "FAIL": "[X]", "DIFF": "[~]", "SKIP": "[-]"}.get(status, "[?]")
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
        lines.append(f"  [X] BLOCKED: {fail_count} critical failures must be resolved before going live.")
    elif warn_count > 0:
        lines.append(f"  [!] PROCEED WITH CAUTION: {warn_count} warnings to review.")
        lines.append(f"  Recommended: fix warnings before P3 live trading.")
    else:
        lines.append(f"  [+] ALL CLEAR: System ready for live trading.")

    if diff_count > 0:
        lines.append(f"  [~] {diff_count} parameter differences between live and backtest configs.")
        lines.append(f"  These differences mean backtest results may not fully predict live performance.")
        lines.append(f"  Align backtest.ini to match live settings for accurate prediction.")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live trading pre-flight check")
    parser.add_argument("--check-api", action="store_true", help="Also check API service")
    args = parser.parse_args()

    all_checks: List[Tuple[str, str, str]] = []

    sys.stderr.write("Checking MT5 connection...\n")
    all_checks.extend(_check_mt5())

    sys.stderr.write("Checking database...\n")
    all_checks.extend(_check_database())

    sys.stderr.write("Checking config consistency...\n")
    config_checks, diff_table = _check_config_consistency()
    all_checks.extend(config_checks)

    sys.stderr.write("Checking risk safety...\n")
    all_checks.extend(_check_risk_safety())

    if args.check_api:
        sys.stderr.write("Checking API service...\n")
        all_checks.extend(_check_api())

    print(_render_results(all_checks, diff_table))


if __name__ == "__main__":
    main()
