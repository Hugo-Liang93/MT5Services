"""置信度管线检查工具 — 模拟当前 regime 下各策略能否通过 min_confidence 门槛。

用法：
    python -m src.ops.cli.confidence_check
    python -m src.ops.cli.confidence_check --tf M15
    python -m src.ops.cli.confidence_check --raw 0.70    # 假设更高的 raw confidence
"""

from __future__ import annotations

import argparse
import os
import sys

# 用 append 而非 insert(0) 避免 src/calendar 阴影 stdlib calendar
# （会破坏 requests/cookiejar `from calendar import timegm` 链路）。
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

import warnings  # noqa: E402

warnings.filterwarnings("ignore")
import logging  # noqa: E402

logging.disable(logging.CRITICAL)

import requests  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Check confidence pipeline pass rates")
    parser.add_argument("--tf", default=None, help="Focus on specific TF")
    parser.add_argument(
        "--raw", type=float, default=0.65, help="Assumed raw confidence (default 0.65)"
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help=(
            "限定 regime_map 取哪个 symbol（缺省 = trading.default_symbol）；"
            "多品种部署里多 symbol 同 TF 不同 regime 会互覆盖，需显式指定"
        ),
    )
    args = parser.parse_args()

    from src.config.signal import get_signal_config

    cfg = get_signal_config()

    overrides = getattr(cfg, "regime_affinity_overrides", {})
    stf = getattr(cfg, "strategy_timeframes", {})
    tf_min_conf = getattr(cfg, "timeframe_min_confidence", {})
    # 真实 live 阈值是 auto_trade_min_confidence（min_preview_confidence 字段已删）
    min_conf_global = getattr(cfg, "auto_trade_min_confidence", 0.55)
    # 部署合同：候选集合必须收口到 deployment.allows_live_execution()
    # 否则 CANDIDATE / DEMO_VALIDATION / PAPER_ONLY 策略会被算进"可通过"造成假阳性
    deployments = getattr(cfg, "strategy_deployments", {}) or {}

    def _strategy_allowed_live(name: str) -> bool:
        if not deployments:
            # 配置完全未启用 deployment 合同：fallback 不过滤（保留旧实现行为）
            return True
        deployment = deployments.get(name)
        if deployment is None:
            # deployment 合同已启用但当前策略未声明：保守拒绝（避免假阳性）
            return False
        return bool(deployment.allows_live_execution())

    def _effective_min_for(name: str, tf: str) -> float:
        """与 pre_trade_checks.py 同语义：
        baseline = timeframe_min_confidence[tf] or auto_trade_min_confidence
        deployment.effective_min_confidence(...) 给出的更高值才生效
        """
        baseline = float(tf_min_conf.get(tf, min_conf_global))
        deployment = deployments.get(name) if deployments else None
        if deployment is None:
            return baseline
        eff = deployment.effective_min_confidence(
            timeframe_baseline=baseline,
            global_min_confidence=float(min_conf_global),
        )
        if eff is None:
            return baseline
        return max(baseline, float(eff))

    # 解析 target symbol：args.symbol > trading.default_symbol > "XAUUSD"
    # 多品种部署里 regime_map 含 SYMBOL/TF 复合键；不限定 symbol 时多 symbol
    # 同 TF 会相互覆盖，诊断结果不确定
    target_symbol: str
    if args.symbol:
        target_symbol = args.symbol.strip().upper()
    else:
        try:
            from src.config.centralized import get_shared_default_symbol

            target_symbol = get_shared_default_symbol().strip().upper()
        except Exception:
            target_symbol = "XAUUSD"

    # 获取当前 regime（从 API）— 仅取 target_symbol 的 (symbol, tf) → regime
    regimes: dict[str, str] = {}
    try:
        r = requests.get("http://localhost:8808/v1/admin/dashboard", timeout=5)
        regime_map = r.json().get("data", {}).get("signals", {}).get("regime_map", {})
        for key, info in regime_map.items():
            if "/" in key:
                sym_part, tf_part = key.split("/", 1)
                if sym_part.strip().upper() != target_symbol:
                    continue
            else:
                # 无 symbol 前缀的旧键格式：仍接受，但只在 single-symbol 部署正确
                tf_part = key
            regimes[tf_part.strip().upper()] = info.get("current_regime", "uncertain")
    except Exception:
        regimes = {
            "M5": "uncertain",
            "M15": "uncertain",
            "M30": "uncertain",
            "H1": "uncertain",
            "H4": "uncertain",
            "D1": "uncertain",
        }

    # P2 回归：strategy_timeframes 的 keys 是策略名而非 TF；默认应从 values 提 TF 集合
    _TF_ORDER = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
    if args.tf:
        tfs = [args.tf]
    else:
        derived = {t.strip().upper() for tfs_list in stf.values() for t in tfs_list}
        tfs = sorted(
            derived,
            key=lambda x: _TF_ORDER.index(x) if x in _TF_ORDER else 99,
        )
    # 去重并取有策略配置的 TF
    seen = set()
    check_tfs = []
    for tf in tfs:
        tf = tf.strip().upper()
        if tf not in seen:
            seen.add(tf)
            check_tfs.append(tf)

    if not check_tfs:
        check_tfs = ["M15", "M30", "H1"]

    raw = args.raw
    print(f"Settings: raw_confidence={raw}, perf=1.0, calibrator=1.0")
    print(f"Per-TF min_confidence: {dict(sorted(tf_min_conf.items()))}")
    print()

    # 收集每个 TF 的 live candidates，便于在 all_blocked 判断里区分
    # "无 live 策略" vs "门槛阻拦"
    live_candidates_by_tf: dict[str, list[str]] = {}
    for tf in check_tfs:
        regime = regimes.get(tf, "uncertain")
        baseline_min = tf_min_conf.get(tf, min_conf_global)
        # 候选 = 配置该 TF 的策略 ∩ deployment 允许 live 执行的策略
        # 后一项守住 candidate / demo_validation / paper_only 不被算进"可通过"
        allowed = [
            name
            for name, tfs_list in stf.items()
            if tf in [t.strip().upper() for t in tfs_list]
            and _strategy_allowed_live(name)
        ]
        live_candidates_by_tf[tf] = allowed

        if not allowed:
            continue

        pass_count = 0
        total = len(allowed)
        lines = []
        for name in sorted(allowed):
            aff_dict = overrides.get(name, {})
            aff = aff_dict.get(regime)
            if aff is None:
                aff = 0.5
                label = "(def)"
            else:
                label = f"{aff:.2f}"
            final = raw * aff
            # 与 pre_trade_checks.py 同源：deployment.min_final_confidence /
            # ACTIVE_GUARDED 推高 baseline → 工具必须叠加才不假 PASS
            effective_min = _effective_min_for(name, tf)
            passed = final >= effective_min
            if passed:
                pass_count += 1
            min_label = f"{effective_min:.2f}"
            if effective_min > baseline_min:
                min_label = f"{effective_min:.2f}*"  # * 标记 deployment 推高
            lines.append(
                f"  {name:<28} aff={label:>6} "
                f"final={final:.3f} min={min_label} "
                f"{'PASS' if passed else 'FAIL'}"
            )

        status = "OK" if pass_count > 0 else "BLOCKED"
        print(
            f"{tf} regime={regime} baseline_min={baseline_min} "
            f"pass={pass_count}/{total} [{status}]"
        )
        for line in lines:
            print(line)
        print()

    # 建议（区分两种空集状态，避免 all([]) → True 误报为"门槛太高"）：
    #  A. 任一 TF 有 live candidate → 进入"BLOCKED 检查"
    #  B. 所有 TF 都没有 live candidate → 真因是无 ACTIVE/ACTIVE_GUARDED 策略
    has_any_live_candidate = any(live_candidates_by_tf.get(tf) for tf in check_tfs)

    if not has_any_live_candidate:
        print(
            "*** NO LIVE-ELIGIBLE STRATEGIES — all current strategies are "
            "CANDIDATE / DEMO_VALIDATION / PAPER_ONLY ***"
        )
        print("Suggestions:")
        print(
            "  1. Promote a strategy to ACTIVE / ACTIVE_GUARDED "
            "(signal.ini [strategy_deployments])"
        )
        print("  2. Verify strategy_deployments map covers strategies in scope")
        print("  3. 调 min_confidence 不会有用 — 没策略能 live 执行")
        return

    all_blocked = all(
        all(
            raw * (overrides.get(name, {}).get(regimes.get(tf, "uncertain"), 0.5))
            < _effective_min_for(name, tf)
            for name in live_candidates_by_tf[tf]
        )
        for tf in check_tfs
        if live_candidates_by_tf.get(tf)
    )
    if all_blocked:
        print(
            "*** ALL TFs BLOCKED — no live-eligible strategy can pass "
            "min_confidence in current regimes ***"
        )
        print("Suggestions:")
        print(
            "  1. Lower per-TF min_confidence (signal.ini [timeframe_min_confidence])"
        )
        print("  2. Raise affinity for strategies in current regimes")
        print("  3. Wait for regime change (current regimes may be unfavorable)")


if __name__ == "__main__":
    main()
