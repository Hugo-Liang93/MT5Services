"""mined_rule_loader: 从 mining JSON 提取 MinedRuleSpec 并按门禁筛选。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from src.signals.strategies.structured.mined_rule import MinedRuleSpec
from src.signals.strategies.structured.mined_rule_loader import (
    PROMOTION_GATES,
    extract_specs_from_mining_json,
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
