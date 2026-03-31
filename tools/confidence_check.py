"""置信度管线检查工具 — 模拟当前 regime 下各策略能否通过 min_confidence 门槛。

用法：
    python tools/confidence_check.py
    python tools/confidence_check.py --tf M15
    python tools/confidence_check.py --raw 0.70    # 假设更高的 raw confidence
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings
warnings.filterwarnings("ignore")
import logging
logging.disable(logging.CRITICAL)

import requests


def main() -> None:
    parser = argparse.ArgumentParser(description="Check confidence pipeline pass rates")
    parser.add_argument("--tf", default=None, help="Focus on specific TF")
    parser.add_argument("--raw", type=float, default=0.65, help="Assumed raw confidence (default 0.65)")
    args = parser.parse_args()

    from src.config.signal import get_signal_config
    cfg = get_signal_config()

    overrides = getattr(cfg, "regime_affinity_overrides", {})
    stf = getattr(cfg, "strategy_timeframes", {})
    tf_min_conf = getattr(cfg, "timeframe_min_confidence", {})
    min_conf_global = getattr(cfg, "min_preview_confidence", 0.55)

    # 获取当前 regime（从 API）
    regimes: dict[str, str] = {}
    try:
        r = requests.get("http://localhost:8808/v1/admin/dashboard", timeout=5)
        regime_map = r.json().get("data", {}).get("signals", {}).get("regime_map", {})
        for key, info in regime_map.items():
            tf_part = key.split("/")[-1] if "/" in key else key
            regimes[tf_part] = info.get("current_regime", "uncertain")
    except Exception:
        regimes = {"M5": "uncertain", "M15": "uncertain", "M30": "uncertain",
                   "H1": "uncertain", "H4": "uncertain", "D1": "uncertain"}

    tfs = [args.tf] if args.tf else sorted(stf.keys(), key=lambda x: ["M1","M5","M15","M30","H1","H4","D1"].index(x) if x in ["M1","M5","M15","M30","H1","H4","D1"] else 99)
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
        allowed = [name for name, tfs_list in stf.items()
                   if tf in [t.strip().upper() for t in tfs_list]]

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
            lines.append(f"  {name:<28} aff={label:>6} final={final:.3f} {'PASS' if passed else 'FAIL'}")

        status = "OK" if pass_count > 0 else "BLOCKED"
        print(f"{tf} regime={regime} min_conf={min_c} pass={pass_count}/{total} [{status}]")
        for line in lines:
            print(line)
        print()

    # 建议
    all_blocked = all(
        all(
            raw * (overrides.get(name, {}).get(regimes.get(tf, "uncertain"), 0.5)) < tf_min_conf.get(tf, min_conf_global)
            for name in [n for n, ts in stf.items() if tf in [t.strip().upper() for t in ts]]
        )
        for tf in check_tfs
        if [n for n, ts in stf.items() if tf in [t.strip().upper() for t in ts]]
    )
    if all_blocked:
        print("*** ALL TFs BLOCKED — no strategy can pass min_confidence in current regimes ***")
        print("Suggestions:")
        print("  1. Lower per-TF min_confidence (signal.ini [timeframe_min_confidence])")
        print("  2. Raise affinity for strategies in current regimes")
        print("  3. Wait for regime change (current regimes may be unfavorable)")


if __name__ == "__main__":
    main()
