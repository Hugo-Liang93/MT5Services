"""诊断工具：为什么没有交易产生。

沿着信号→交易的完整链路逐层检查，找出信号在哪个环节被拦截。
一次性输出所有诊断结果，不需要反复 API 调用。

用法：
    python -m src.ops.cli.diagnose_no_trades
    python -m src.ops.cli.diagnose_no_trades --tf M15
    python -m src.ops.cli.diagnose_no_trades --tf H1 --hours 24
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
warnings.filterwarnings("ignore")
import logging
logging.disable(logging.CRITICAL)

from datetime import datetime, timezone, timedelta
from collections import defaultdict


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose why no trades are generated")
    parser.add_argument("--tf", default=None, help="Focus on specific timeframe (e.g. M15)")
    parser.add_argument("--hours", type=int, default=24, help="Look back N hours")
    args = parser.parse_args()

    import requests
    BASE = "http://localhost:8808"

    # 1. Health check
    try:
        r = requests.get(f"{BASE}/health", timeout=5)
        health = r.json()["data"]
        print("1. SERVICE: OK")
    except Exception as e:
        print(f"1. SERVICE: DOWN ({e})")
        return

    # 2. MT5 connection
    mt5 = health["market"]
    print(f"2. MT5: {'connected' if mt5['connected'] else 'DISCONNECTED'} | {mt5.get('server','?')}")

    # 3. Account
    try:
        r = requests.get(f"{BASE}/v1/account/info", timeout=5)
        acc = r.json()["data"]
        print(f"3. ACCOUNT: balance=${acc['balance']:.2f} equity=${acc['equity']:.2f}")
    except:
        print("3. ACCOUNT: failed to fetch")

    # 4. Dashboard data
    try:
        r = requests.get(f"{BASE}/v1/admin/dashboard", timeout=5)
        dash = r.json()["data"]
    except:
        print("4. DASHBOARD: failed to fetch")
        return

    # 5. Indicator Manager
    ind = dash.get("indicators", {})
    scope_stats = ind.get("scope_stats", {})
    confirmed_calcs = scope_stats.get("confirmed", {}).get("computations", 0)
    intrabar_calcs = scope_stats.get("intrabar", {}).get("computations", 0)
    print(f"4. INDICATORS: confirmed={confirmed_calcs} intrabar={intrabar_calcs} event_loop={ind.get('event_loop_running')}")

    if confirmed_calcs == 0:
        print("   *** PROBLEM: No confirmed bar events processed. Check OHLC ingestion. ***")

    # 6. SignalRuntime
    sig = dash.get("signals", {})
    warmup = sig.get("warmup", {})
    processing = sig.get("processing", {})
    queues = sig.get("queues", {})
    filters = sig.get("filters", {})

    total_processed = processing.get("processed_events", 0)
    print(f"5. SIGNAL RUNTIME: processed={total_processed} warmup_ready={warmup.get('ready')}")

    if not warmup.get("ready"):
        print("   *** PROBLEM: Warmup not ready. System is still in warmup phase. ***")

    # 7. Filter stats
    by_scope = filters.get("by_scope", {})
    confirmed_filter = by_scope.get("confirmed", {})
    filter_passed = confirmed_filter.get("passed", 0)
    filter_blocked = confirmed_filter.get("blocked", 0)
    filter_blocks = confirmed_filter.get("blocks", {})
    affinity_skipped = filters.get("affinity_gates_skipped", 0)

    print(f"6. FILTERS: passed={filter_passed} blocked={filter_blocked} affinity_skipped={affinity_skipped}")
    if filter_blocks:
        for reason, count in filter_blocks.items():
            print(f"   block: {reason} = {count}")
    if affinity_skipped > 0:
        pct = affinity_skipped / (filter_passed + affinity_skipped) * 100 if (filter_passed + affinity_skipped) > 0 else 0
        print(f"   affinity skip rate: {pct:.1f}% ({affinity_skipped}/{filter_passed + affinity_skipped})")

    # 8. Regime per TF
    regime_map = sig.get("regime_map", {})
    print(f"7. REGIME:")
    for key, regime_info in sorted(regime_map.items()):
        r_type = regime_info.get("current_regime", "?")
        bars = regime_info.get("consecutive_bars", 0)
        tf_part = key.split("/")[-1] if "/" in key else key
        if args.tf and tf_part != args.tf:
            continue
        print(f"   {key}: {r_type} ({bars} bars)")

    # 9. Active signal states (non-hold)
    active_states = sig.get("active_states", {})
    confirmed_count = active_states.get("confirmed", 0)
    print(f"8. ACTIVE STATES: confirmed={confirmed_count} preview={active_states.get('preview', 0)}")

    # 10. Executor
    ex = dash.get("executor", {})
    print(f"9. EXECUTOR: enabled={ex.get('enabled')} executions={ex.get('execution_count', 0)} circuit={ex.get('circuit_open', False)}")
    pending = ex.get("pending_entries_count", 0)
    if pending > 0:
        print(f"   pending entries: {pending}")

    # 11. Position Manager
    pm = dash.get("positions", {})
    mgr = pm.get("manager", {})
    eod_date = mgr.get("last_end_of_day_close_date")
    print(f"10. POSITIONS: tracked={mgr.get('tracked_positions', 0)} eod_date={eod_date}")
    if eod_date == datetime.now(timezone.utc).date().isoformat():
        print("   *** INFO: EOD already executed today. No new trades will be opened. ***")

    # 12. Check signal_events in DB for actual signals produced
    print(f"\n11. DB SIGNAL EVENTS (last {args.hours}h):")
    try:
        r = requests.get(f"{BASE}/v1/signals/latest?symbol=XAUUSD&limit=50", timeout=5)
        events = r.json().get("data", [])
        if not events:
            print("   No signal events found in DB")
        else:
            # Count by state
            state_counts = defaultdict(int)
            tf_counts = defaultdict(lambda: defaultdict(int))
            for e in events:
                state = e.get("signal_state", "?")
                state_counts[state] += 1
                tf = e.get("timeframe", "?")
                tf_counts[tf][state] += 1

            for state, count in sorted(state_counts.items(), key=lambda x: -x[1]):
                print(f"   {state}: {count}")

            if args.tf:
                tf_data = tf_counts.get(args.tf, {})
                if tf_data:
                    print(f"   [{args.tf}] breakdown: {dict(tf_data)}")
                else:
                    print(f"   [{args.tf}] no events for this TF")
    except Exception as e:
        print(f"   Failed to query: {e}")

    # 13. Summary diagnosis
    print(f"\n{'='*60}")
    print("DIAGNOSIS SUMMARY:")
    problems = []

    if confirmed_calcs == 0:
        problems.append("No confirmed bar events → indicators not computing → no signals")
    if not warmup.get("ready"):
        problems.append("Warmup not ready → signals suppressed")
    if filter_passed == 0 and filter_blocked > 0:
        problems.append(f"All {filter_blocked} signals blocked by filters ({filter_blocks})")
    if affinity_skipped > filter_passed * 2:
        problems.append(f"Affinity gate skipping {affinity_skipped} evaluations — regime affinities too aggressive")
    if eod_date == datetime.now(timezone.utc).date().isoformat():
        problems.append("EOD already fired today → after_eod_block prevents new trades")
    if confirmed_count == 0 and filter_passed > 0:
        problems.append("Filters pass signals but no confirmed states → all strategies output hold or confidence too low")
    if ex.get("circuit_open"):
        problems.append("Executor circuit breaker is OPEN")
    if ex.get("execution_count", 0) == 0 and confirmed_count > 0:
        problems.append("Active confirmed states exist but executor has 0 executions → signals not reaching executor (voting quorum? min_confidence?)")

    # Check regime-specific issues
    for key, regime_info in regime_map.items():
        r_type = regime_info.get("current_regime", "?")
        bars = regime_info.get("consecutive_bars", 0)
        if r_type == "ranging" and bars > 100:
            problems.append(f"{key}: ranging for {bars} bars — most strategies have near-zero affinity")
        if r_type == "uncertain" and bars > 50:
            problems.append(f"{key}: uncertain for {bars} bars — many strategies suppressed")

    if not problems:
        problems.append("No obvious problems detected — system may just not have found qualifying setups")

    for i, p in enumerate(problems, 1):
        print(f"  {i}. {p}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

