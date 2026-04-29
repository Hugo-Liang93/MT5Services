"""mined_rule_loader: 从 mining JSON 提取 MinedRuleSpec 并按门禁筛选。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from src.signals.strategies.structured.mined_rule import MinedRuleSpec
from src.signals.strategies.structured.mined_rule_loader import (
    BACKTEST_VERIFICATION_GATES,
    PROMOTION_GATES,
    extract_specs_from_mining_json,
    filter_by_backtest,
    filter_promotable,
    load_specs_from_path,
)


def _make_mining_payload(rules):
    """构造最小的 mining JSON payload。"""
    return {
        "results": [
            {
                "tf": "H1",
                "run_id": "mine_test_run",
                "mined_rules": rules,
            }
        ]
    }


def _good_rule(direction="buy", train_wr=0.65, test_wr=0.55, train_n=200, test_n=80):
    """符合 promotion 门禁的 rule。"""
    return {
        "direction": direction,
        "train": {
            "hit_rate": train_wr,
            "n_samples": train_n,
            "mean_return": 0.005,
        },
        "test": {"hit_rate": test_wr, "n_samples": test_n},
        "structured": {
            "why": [
                {
                    "indicator": "adx14",
                    "field": "minus_di",
                    "operator": "<=",
                    "threshold": 19.38,
                }
            ],
            "when": [],
            "where": [],
        },
        "barrier_stats_train": [
            {
                "sl_atr": 2.0,
                "tp_atr": 3.0,
                "time_bars": 80,
                "hit_rate": 0.56,
                "n": 195,
            }
        ],
    }


# ── extract_specs_from_mining_json ──────────────────────────────────


def test_extract_specs_returns_list_of_specs() -> None:
    payload = _make_mining_payload([_good_rule()])
    specs = extract_specs_from_mining_json(payload)
    assert len(specs) == 1
    assert isinstance(specs[0], MinedRuleSpec)


def test_extract_spec_name_includes_tf_direction_index() -> None:
    payload = _make_mining_payload(
        [_good_rule(direction="buy"), _good_rule(direction="sell")]
    )
    specs = extract_specs_from_mining_json(payload)
    names = [s.name for s in specs]
    # 名字格式：structured_mined_<tf>_<direction>_<idx>
    assert any("h1_buy" in n.lower() for n in names)
    assert any("h1_sell" in n.lower() for n in names)


def test_extract_propagates_run_id_into_spec_provenance() -> None:
    payload = _make_mining_payload([_good_rule()])
    specs = extract_specs_from_mining_json(payload)
    assert specs[0].mining_run_id == "mine_test_run"


def test_extract_picks_top_barrier_for_exit_spec() -> None:
    """spec.barrier 取 barrier_stats_train 第一条（mining 已按 hit_rate 排序）。"""
    payload = _make_mining_payload([_good_rule()])
    specs = extract_specs_from_mining_json(payload)
    assert specs[0].barrier.sl_atr == 2.0
    assert specs[0].barrier.tp_atr == 3.0
    assert specs[0].barrier.time_bars == 80


def test_extract_skips_rule_without_barrier_stats() -> None:
    rule = _good_rule()
    rule["barrier_stats_train"] = []
    payload = _make_mining_payload([rule])
    specs = extract_specs_from_mining_json(payload)
    assert specs == []


def test_extract_handles_multi_tf_payload() -> None:
    payload = {
        "results": [
            {"tf": "H1", "run_id": "mine_h1", "mined_rules": [_good_rule()]},
            {"tf": "M30", "run_id": "mine_m30", "mined_rules": [_good_rule()]},
        ]
    }
    specs = extract_specs_from_mining_json(payload)
    tfs = {s.timeframe for s in specs}
    assert tfs == {"H1", "M30"}


# ── filter_promotable: 多门禁筛选 ────────────────────────────────────


def test_filter_keeps_rule_passing_all_gates() -> None:
    spec = extract_specs_from_mining_json(_make_mining_payload([_good_rule()]))[0]
    promoted = filter_promotable([spec])
    assert len(promoted) == 1


def test_filter_rejects_low_train_wr() -> None:
    rule = _good_rule(train_wr=0.50)  # < 0.55 floor
    spec = extract_specs_from_mining_json(_make_mining_payload([rule]))[0]
    assert filter_promotable([spec]) == []


def test_filter_rejects_low_test_wr() -> None:
    rule = _good_rule(test_wr=0.45)  # < 0.52 floor
    spec = extract_specs_from_mining_json(_make_mining_payload([rule]))[0]
    assert filter_promotable([spec]) == []


def test_filter_rejects_train_test_drop_too_steep() -> None:
    """train→test 衰减 > 30% 视为过拟合。"""
    rule = _good_rule(train_wr=0.80, test_wr=0.53)  # 33% 衰减
    spec = extract_specs_from_mining_json(_make_mining_payload([rule]))[0]
    assert filter_promotable([spec]) == []


def test_filter_rejects_low_test_n() -> None:
    rule = _good_rule(test_n=20)  # < 30 floor
    spec = extract_specs_from_mining_json(_make_mining_payload([rule]))[0]
    assert filter_promotable([spec]) == []


def test_filter_rejects_negative_mean_return() -> None:
    rule = _good_rule()
    rule["train"]["mean_return"] = -0.001
    spec = extract_specs_from_mining_json(_make_mining_payload([rule]))[0]
    assert filter_promotable([spec]) == []


def test_filter_rejects_low_barrier_wr() -> None:
    rule = _good_rule()
    rule["barrier_stats_train"][0]["hit_rate"] = 0.45  # < 0.50 floor
    spec = extract_specs_from_mining_json(_make_mining_payload([rule]))[0]
    assert filter_promotable([spec]) == []


def test_filter_thresholds_documented() -> None:
    """PROMOTION_GATES 应有清晰的阈值集合便于审计。"""
    assert "min_train_wr" in PROMOTION_GATES
    assert "min_test_wr" in PROMOTION_GATES
    assert "min_test_n" in PROMOTION_GATES
    assert "max_train_test_drop" in PROMOTION_GATES
    assert "min_barrier_wr" in PROMOTION_GATES
    assert "min_mean_return" in PROMOTION_GATES


# ── load_specs_from_path ─────────────────────────────────────────────


def test_load_specs_from_path(tmp_path: Path) -> None:
    payload = _make_mining_payload([_good_rule()])
    json_path = tmp_path / "mining.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")

    specs = load_specs_from_path(json_path)

    assert len(specs) == 1
    assert isinstance(specs[0], MinedRuleSpec)


# ── filter_by_backtest: realized-execution gate ───────────────────────


def _spec(name: str, tf: str = "H4") -> MinedRuleSpec:
    """构造 minimal MinedRuleSpec（绕过 mining JSON 路径直接喂 filter）。"""
    payload = _make_mining_payload([_good_rule()])
    payload["results"][0]["tf"] = tf
    payload["results"][0]["mined_rules"][0]["direction"] = "buy"
    spec = extract_specs_from_mining_json(payload)[0]
    # 重写 name 以匹配测试需要
    return MinedRuleSpec(
        name=name,
        direction=spec.direction,
        timeframe=spec.timeframe,
        conditions=spec.conditions,
        barrier=spec.barrier,
        mining_run_id=spec.mining_run_id,
        train_wr=spec.train_wr,
        test_wr=spec.test_wr,
        train_n=spec.train_n,
        test_n=spec.test_n,
        barrier_wr=spec.barrier_wr,
        train_mean_return=spec.train_mean_return,
    )


def test_filter_by_backtest_keeps_profitable_high_wr_spec() -> None:
    """实测 PnL 正 + WR>=floor + 样本数足 → 通过。"""
    spec = _spec("structured_mined_h4_buy_1")
    stats = {"structured_mined_h4_buy_1": {"n": 73, "w": 37, "pnl": 108.0}}
    out = filter_by_backtest(
        [spec],
        stats,
        min_realized_wr=0.45,
        min_realized_pnl=0.0,
        min_realized_n=20,
    )
    assert [s.name for s in out] == ["structured_mined_h4_buy_1"]


def test_filter_by_backtest_drops_negative_pnl_even_if_high_wr() -> None:
    """h4_sell_2 实测 WR=30.8% (mining 58%) PnL=-76 → 应被丢。"""
    spec = _spec("structured_mined_h4_sell_2")
    stats = {"structured_mined_h4_sell_2": {"n": 39, "w": 12, "pnl": -75.5}}
    out = filter_by_backtest(
        [spec],
        stats,
        min_realized_wr=0.45,
        min_realized_pnl=0.0,
        min_realized_n=20,
    )
    assert out == []


def test_filter_by_backtest_drops_zero_trade_specs() -> None:
    """h1_buy_5 / m30_sell_2 实测 0 trades → 应被丢（无样本无证据）。"""
    spec = _spec("structured_mined_h1_buy_5")
    out = filter_by_backtest(
        [spec],
        {},  # 没有该 spec 的 stats
        min_realized_wr=0.45,
        min_realized_pnl=0.0,
        min_realized_n=20,
    )
    assert out == []


def test_filter_by_backtest_drops_below_min_n() -> None:
    """实测 n<floor → 样本太少不足以判断，丢。"""
    spec = _spec("structured_mined_h4_buy_1")
    stats = {"structured_mined_h4_buy_1": {"n": 10, "w": 8, "pnl": 50.0}}
    out = filter_by_backtest(
        [spec],
        stats,
        min_realized_wr=0.45,
        min_realized_pnl=0.0,
        min_realized_n=20,
    )
    assert out == []


def test_filter_by_backtest_drops_below_min_wr() -> None:
    """h1_buy_3 实测 WR=44.3% < 45% floor → 丢。"""
    spec = _spec("structured_mined_h1_buy_3")
    stats = {"structured_mined_h1_buy_3": {"n": 70, "w": 31, "pnl": -39.5}}
    out = filter_by_backtest(
        [spec],
        stats,
        min_realized_wr=0.45,
        min_realized_pnl=0.0,
        min_realized_n=20,
    )
    assert out == []


def test_filter_by_backtest_thresholds_documented() -> None:
    """BACKTEST_VERIFICATION_GATES 应有清晰阈值集合便于审计。"""
    assert "min_realized_wr" in BACKTEST_VERIFICATION_GATES
    assert "min_realized_pnl" in BACKTEST_VERIFICATION_GATES
    assert "min_realized_n" in BACKTEST_VERIFICATION_GATES
