"""Regression tests for src/ops/cli/confidence_check.py.

P1: sys.path.insert(0, repo_root) 把 src/ 提到 stdlib 之前 → src/calendar 包
    阴影 stdlib calendar → import requests 触发的 http.cookiejar →
    `from calendar import timegm` 抛 ImportError。

P2#3: 默认路径下 args.tf 缺省时拿 stf.keys()（策略名）当 TF 列表，
    后续匹配永远空 → 脚本只打 settings 不输出 TF 诊断。

P2#4: 候选集合不收口 strategy_deployments；CANDIDATE / DEMO_VALIDATION
    策略本不能 live 执行但被算进 "可通过"（PAPER_ONLY 已在 ADR-010 后从 enum 删除）。
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_confidence_check_help_does_not_shadow_stdlib_calendar() -> None:
    """P1 回归：脚本启动期 import requests 不应被 src/calendar 阴影。

    在子进程执行 `python -m src.ops.cli.confidence_check --help`，
    旧实现因 sys.path.insert(0, repo_root) 让 src/calendar 优先于 stdlib，
    requests/compat.py 的 `from calendar import timegm` 立即崩。
    """
    result = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.confidence_check", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (
        "ImportError" not in result.stderr
    ), f"sys.path 阴影 stdlib calendar 复现：\n{result.stderr}"
    assert "cannot import name 'timegm'" not in result.stderr
    assert result.returncode == 0, f"--help 应直接返回；实际 stderr:\n{result.stderr}"


def test_diagnose_no_trades_help_does_not_shadow_stdlib_calendar() -> None:
    """P1 同源：diagnose_no_trades 同样的 sys.path 阴影问题。"""
    result = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.diagnose_no_trades", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (
        "ImportError" not in result.stderr
    ), f"sys.path 阴影 stdlib calendar 复现：\n{result.stderr}"
    assert "cannot import name 'timegm'" not in result.stderr
    assert result.returncode == 0


def test_confidence_check_no_sys_path_insert_at_index_zero() -> None:
    """sentinel：禁止 sys.path.insert(0, ...) — 该 hack 让 src/* 包阴影 stdlib。

    应改用 sys.path.append（stdlib 优先）。
    """
    from src.ops.cli import confidence_check

    source = inspect.getsource(confidence_check)
    assert "sys.path.insert(0" not in source, (
        "禁止 sys.path.insert(0, ...) — 会让 src/calendar 阴影 stdlib calendar "
        "（破坏 requests/cookiejar 链路）；应改用 sys.path.append"
    )


def test_diagnose_no_trades_no_sys_path_insert_at_index_zero() -> None:
    """sentinel：同 confidence_check。"""
    from src.ops.cli import diagnose_no_trades

    source = inspect.getsource(diagnose_no_trades)
    assert "sys.path.insert(0" not in source


# ---------------------------------------------------------------------------
# P2 #3: 默认 TF 推导错误
# ---------------------------------------------------------------------------


def test_confidence_check_default_derives_tf_from_strategy_values_not_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """回归：默认路径下 (no --tf) 应从 strategy_timeframes 的 **values**
    （TF 列表）取 TF 集合，而非 keys（策略名）。

    旧实现：tfs = sorted(stf.keys(), ...)，得到 STRUCTURED_TREND_CONTINUATION
    等策略名 → 当 TF 用 → 永远匹配 0 个 allowed → 啥也不打印。
    """
    from src.ops.cli import confidence_check

    # 模拟 SignalConfig
    class _FakeCfg:
        strategy_timeframes = {
            "STRUCTURED_TREND_CONTINUATION": ["M15", "M30", "H1"],
            "STRUCTURED_BREAKOUT_FOLLOW": ["M30", "H1"],
        }
        regime_affinity_overrides = {
            "STRUCTURED_TREND_CONTINUATION": {"trending_up": 1.0},
            "STRUCTURED_BREAKOUT_FOLLOW": {"trending_up": 1.0},
        }
        timeframe_min_confidence = {"M15": 0.5, "M30": 0.5, "H1": 0.5}
        auto_trade_min_confidence = 0.5
        # 防止 deployment filter 把所有过滤掉（设为 ACTIVE）
        strategy_deployments: dict = {}

    monkeypatch.setattr("src.config.signal.get_signal_config", lambda: _FakeCfg())
    # 防止真实 HTTP 调用
    import requests as _req

    class _FakeResp:
        def json(self):
            return {"data": {}}

    monkeypatch.setattr(_req, "get", lambda *a, **kw: _FakeResp())
    monkeypatch.setattr("sys.argv", ["confidence_check"])

    confidence_check.main()
    out = capsys.readouterr().out

    # 必须打印 TF 段（M15 / M30 / H1 之一），而非纯 settings
    assert any(
        f"{tf} regime=" in out for tf in ("M15", "M30", "H1")
    ), f"默认路径必须输出至少一个 TF 段；实际输出:\n{out}"
    # 旧 bug 表现：策略名出现在 TF 段标题
    assert "STRUCTURED_TREND_CONTINUATION regime=" not in out
    assert "STRUCTURED_BREAKOUT_FOLLOW regime=" not in out


# ---------------------------------------------------------------------------
# P2 #4: 不收口 deployment.allows_live_execution()
# ---------------------------------------------------------------------------


def test_confidence_check_filters_by_deployment_allows_live(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """回归：候选集合应过滤掉不能 live 执行的策略
    （CANDIDATE / DEMO_VALIDATION；PAPER_ONLY 历史 enum 已删）。

    否则即便算出某 TF "有策略能 PASS"，对 live 结论也是假阳性。
    """
    from src.ops.cli import confidence_check
    from src.signals.contracts.deployment import (
        StrategyDeployment,
        StrategyDeploymentStatus,
    )

    class _FakeCfg:
        strategy_timeframes = {
            "trend_active": ["M30"],
            "candidate_xyz": ["M30"],
            "demo_only": ["M30"],
        }
        regime_affinity_overrides = {
            "trend_active": {"trending_up": 1.0},
            "candidate_xyz": {"trending_up": 1.0},
            "demo_only": {"trending_up": 1.0},
        }
        timeframe_min_confidence = {"M30": 0.3}
        auto_trade_min_confidence = 0.3
        strategy_deployments = {
            "trend_active": StrategyDeployment(
                name="trend_active", status=StrategyDeploymentStatus.ACTIVE
            ),
            "candidate_xyz": StrategyDeployment(
                name="candidate_xyz",
                status=StrategyDeploymentStatus.CANDIDATE,
            ),
            "demo_only": StrategyDeployment(
                name="demo_only",
                status=StrategyDeploymentStatus.DEMO_VALIDATION,
            ),
        }

    monkeypatch.setattr("src.config.signal.get_signal_config", lambda: _FakeCfg())
    import requests as _req

    class _FakeResp:
        def json(self):
            return {
                "data": {
                    "signals": {
                        "regime_map": {"XAUUSD/M30": {"current_regime": "trending_up"}}
                    }
                }
            }

    monkeypatch.setattr(_req, "get", lambda *a, **kw: _FakeResp())
    monkeypatch.setattr("sys.argv", ["confidence_check", "--tf", "M30"])

    confidence_check.main()
    out = capsys.readouterr().out

    # 只有 trend_active（ACTIVE）应出现在 PASS/FAIL 行
    assert "trend_active" in out
    # CANDIDATE / DEMO_VALIDATION 必须被过滤
    assert (
        "candidate_xyz" not in out
    ), f"CANDIDATE 策略不能 live 执行，必须被过滤；输出:\n{out}"
    assert (
        "demo_only" not in out
    ), f"DEMO_VALIDATION 策略不能 live 执行，必须被过滤；输出:\n{out}"


# ---------------------------------------------------------------------------
# P2#1: all_blocked 误报 — 无 live 策略时 all([]) → True → "ALL TFs BLOCKED"
# ---------------------------------------------------------------------------


def test_no_live_eligible_strategies_does_not_misreport_all_blocked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """回归：当全部策略 = CANDIDATE/DEMO_VALIDATION 时（PAPER_ONLY 历史 enum 已删），
    每个 TF 的 _live_candidates 都是空列表，原 all(...) 落在空生成器上返 True
    → 输出 "ALL TFs BLOCKED" + "调低 min_confidence" 建议。
    真因是无 live-eligible 策略；建议方向应该是推策略到 ACTIVE_GUARDED。
    """
    from src.ops.cli import confidence_check
    from src.signals.contracts.deployment import (
        StrategyDeployment,
        StrategyDeploymentStatus,
    )

    class _FakeCfg:
        strategy_timeframes = {
            "candidate_a": ["M30"],
            "demo_b": ["M30"],
        }
        regime_affinity_overrides = {}
        timeframe_min_confidence = {"M30": 0.5}
        auto_trade_min_confidence = 0.5
        strategy_deployments = {
            "candidate_a": StrategyDeployment(
                name="candidate_a", status=StrategyDeploymentStatus.CANDIDATE
            ),
            "demo_b": StrategyDeployment(
                name="demo_b", status=StrategyDeploymentStatus.DEMO_VALIDATION
            ),
        }

    monkeypatch.setattr("src.config.signal.get_signal_config", lambda: _FakeCfg())
    import requests as _req

    class _FakeResp:
        def json(self):
            return {"data": {}}

    monkeypatch.setattr(_req, "get", lambda *a, **kw: _FakeResp())
    monkeypatch.setattr("sys.argv", ["confidence_check", "--tf", "M30"])

    confidence_check.main()
    out = capsys.readouterr().out

    assert (
        "ALL TFs BLOCKED" not in out
    ), f"无 live-eligible 策略 ≠ 阈值太高；不应建议调 min_confidence；输出:\n{out}"
    assert (
        "NO LIVE-ELIGIBLE STRATEGIES" in out or "no live-eligible" in out.lower()
    ), f"应明确提示无 live 策略，引导用户改 deployment status；输出:\n{out}"


# ---------------------------------------------------------------------------
# P2#2: deployment.effective_min_confidence 未叠加 — guarded/min_final 假 PASS
# ---------------------------------------------------------------------------


def test_effective_min_confidence_threaded_for_guarded_strategy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """回归：ACTIVE_GUARDED 且 min_final_confidence=0.8 的策略，
    pre_trade_checks 会把门槛抬到 0.8；工具应同步叠加，否则给假 PASS。

    raw=0.65 affinity=1.0 min_c(timeframe)=0.5 → 旧实现报 PASS（0.65 ≥ 0.5）
    但实际执行用 effective_min=max(0.5, 0.8)=0.8 → 真实 FAIL
    """
    from src.ops.cli import confidence_check
    from src.signals.contracts.deployment import (
        StrategyDeployment,
        StrategyDeploymentStatus,
    )

    class _FakeCfg:
        strategy_timeframes = {"guarded_strict": ["M30"]}
        regime_affinity_overrides = {"guarded_strict": {"trending_up": 1.0}}
        timeframe_min_confidence = {"M30": 0.5}
        auto_trade_min_confidence = 0.5
        strategy_deployments = {
            "guarded_strict": StrategyDeployment(
                name="guarded_strict",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
                min_final_confidence=0.8,
            ),
        }

    monkeypatch.setattr("src.config.signal.get_signal_config", lambda: _FakeCfg())
    import requests as _req

    class _FakeResp:
        def json(self):
            return {
                "data": {
                    "signals": {
                        "regime_map": {"XAUUSD/M30": {"current_regime": "trending_up"}}
                    }
                }
            }

    monkeypatch.setattr(_req, "get", lambda *a, **kw: _FakeResp())
    monkeypatch.setattr(
        "sys.argv", ["confidence_check", "--raw", "0.65", "--tf", "M30"]
    )

    confidence_check.main()
    out = capsys.readouterr().out

    # 真实 effective min = 0.8（来自 min_final_confidence），final=0.65*1.0=0.65 < 0.8
    # → 必须 FAIL，且最好显示 effective threshold（0.8）而非 baseline 0.5
    assert "guarded_strict" in out
    # 不应该有 PASS 标记
    assert "guarded_strict" + " " * 19 + "aff=  1.00 final=0.650 PASS" not in out
    # 应有 FAIL（精确格式可能因 min 显示方式略变；确认非 PASS）
    guarded_lines = [line for line in out.splitlines() if "guarded_strict" in line]
    assert any(
        "FAIL" in line for line in guarded_lines
    ), f"effective_min=0.8，0.65 必须 FAIL；输出:\n{out}"


# ---------------------------------------------------------------------------
# P2#3: regime_map 多 symbol 同 TF 覆盖
# ---------------------------------------------------------------------------


def test_regime_map_isolates_by_symbol_via_explicit_arg(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """回归：dashboard 返回 XAUUSD/M30=trending_up + EURUSD/M30=ranging，
    旧实现压平到 regimes[M30]=最后一个 → 多品种部署诊断结果不确定。

    工具应支持 --symbol（缺省读 default_symbol）并仅取该 symbol 的 regime。
    """
    from src.ops.cli import confidence_check
    from src.signals.contracts.deployment import (
        StrategyDeployment,
        StrategyDeploymentStatus,
    )

    class _FakeCfg:
        strategy_timeframes = {"trend_strategy": ["M30"]}
        regime_affinity_overrides = {
            "trend_strategy": {"trending_up": 1.0, "ranging": 0.0}
        }
        timeframe_min_confidence = {"M30": 0.5}
        auto_trade_min_confidence = 0.5
        strategy_deployments = {
            "trend_strategy": StrategyDeployment(
                name="trend_strategy", status=StrategyDeploymentStatus.ACTIVE
            ),
        }

    monkeypatch.setattr("src.config.signal.get_signal_config", lambda: _FakeCfg())
    import requests as _req

    # dashboard 返多 symbol；应用 --symbol XAUUSD 隔离
    class _FakeResp:
        def json(self):
            return {
                "data": {
                    "signals": {
                        "regime_map": {
                            "XAUUSD/M30": {"current_regime": "trending_up"},
                            "EURUSD/M30": {"current_regime": "ranging"},
                        }
                    }
                }
            }

    monkeypatch.setattr(_req, "get", lambda *a, **kw: _FakeResp())
    monkeypatch.setattr(
        "sys.argv", ["confidence_check", "--symbol", "XAUUSD", "--tf", "M30"]
    )

    confidence_check.main()
    out = capsys.readouterr().out

    # XAUUSD/M30=trending_up affinity=1.0 final=0.65*1.0=0.65 ≥ 0.5 → PASS
    # 旧 bug：M30=ranging（被 EURUSD 覆盖）affinity=0.0 → final=0 < 0.5 → BLOCKED
    assert (
        "regime=trending_up" in out
    ), f"应取 XAUUSD/M30 的 trending_up，旧 bug 取 EURUSD 的 ranging；输出:\n{out}"
    assert "[OK]" in out, f"trending_up 下 trend_strategy 应 PASS；输出:\n{out}"
