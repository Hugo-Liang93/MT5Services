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

    # 获取当前 regime（从 API）
    regimes: dict[str, str] = {}
    try:
        r = requests.get("http://localhost:8808/v1/admin/dashboard", timeout=5)
        regime_map = r.json().get("data", {}).get("signals", {}).get("regime_map", {})
        for key, info in regime_map.items():
            tf_part = key.split("/")[-1] if "/" in key else key
            regimes[tf_part] = info.get("current_regime", "uncertain")
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

    for tf in check_tfs:
        regime = regimes.get(tf, "uncertain")
        min_c = tf_min_conf.get(tf, min_conf_global)
        # 候选 = 配置该 TF 的策略 ∩ deployment 允许 live 执行的策略
        # 后一项守住 candidate / demo_validation / paper_only 不被算进"可通过"
        allowed = [
            name
            for name, tfs_list in stf.items()
            if tf in [t.strip().upper() for t in tfs_list]
            and _strategy_allowed_live(name)
        ]

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
            passed = final >= min_c
            if passed:
                pass_count += 1
            lines.append(
                f"  {name:<28} aff={label:>6} final={final:.3f} {'PASS' if passed else 'FAIL'}"
            )

        status = "OK" if pass_count > 0 else "BLOCKED"
        print(
            f"{tf} regime={regime} min_conf={min_c} pass={pass_count}/{total} [{status}]"
        )
        for line in lines:
            print(line)
        print()

    # 建议（同样收口 deployment 允许 live 执行的策略）
    def _live_candidates(tf: str) -> list[str]:
        return [
            n
            for n, ts in stf.items()
            if tf in [t.strip().upper() for t in ts] and _strategy_allowed_live(n)
        ]

    all_blocked = all(
        all(
            raw * (overrides.get(name, {}).get(regimes.get(tf, "uncertain"), 0.5))
            < tf_min_conf.get(tf, min_conf_global)
            for name in _live_candidates(tf)
        )
        for tf in check_tfs
        if _live_candidates(tf)
    )
    if all_blocked:
        print(
            "*** ALL TFs BLOCKED — no strategy can pass min_confidence in current regimes ***"
        )
        print("Suggestions:")
        print(
            "  1. Lower per-TF min_confidence (signal.ini [timeframe_min_confidence])"
        )
        print("  2. Raise affinity for strategies in current regimes")
        print("  3. Wait for regime change (current regimes may be unfavorable)")


if __name__ == "__main__":
    main()
