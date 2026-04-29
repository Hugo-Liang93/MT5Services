"""catalog.register_mined_rule_strategies 测试。"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

from src.signals.strategies.base import SignalStrategy
from src.signals.strategies.catalog import (
    build_named_strategy_catalog,
    register_mined_rule_strategies,
)
from src.signals.strategies.structured.mined_rule import MinedRuleStrategy


def _make_payload(name_idx: int = 0, train_wr: float = 0.65, test_wr: float = 0.55):
    """构造单 rule mining payload，可调 wr 控制是否通过 promotion gate。"""
    return {
        "results": [
            {
                "tf": "H1",
                "run_id": "mine_test",
                "mined_rules": [
                    {
                        "direction": "buy",
                        "train": {
                            "hit_rate": train_wr,
                            "n_samples": 200,
                            "mean_return": 0.005,
                        },
                        "test": {"hit_rate": test_wr, "n_samples": 80},
                        "structured": {
                            "why": [
                                {
                                    "indicator": "adx14",
                                    "field": "adx",
                                    "operator": ">",
                                    "threshold": 20.0,
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
                ],
            }
        ]
    }


def _write_payload(tmp_path: Path, payload, name="mining.json"):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ── Default catalog 不含 mined_rule（向后兼容）──────────────────────


def test_default_catalog_excludes_mined_rules() -> None:
    catalog = build_named_strategy_catalog()
    for name, strat in catalog.items():
        assert not isinstance(strat, MinedRuleStrategy), (
            f"默认 catalog 不应含 MinedRuleStrategy 实例（{name}）；"
            f"装配层应显式调用 register_mined_rule_strategies"
        )


# ── register helper：核心契约 ────────────────────────────────────────


def test_register_adds_promoted_specs(tmp_path: Path) -> None:
    json_path = _write_payload(tmp_path, _make_payload())

    catalog: "OrderedDict[str, SignalStrategy]" = OrderedDict()
    n = register_mined_rule_strategies(catalog, [json_path])

    assert n == 1
    assert any(name.startswith("structured_mined_") for name in catalog)


def test_register_skips_specs_failing_promotion_gate(tmp_path: Path) -> None:
    """train_wr 50% < 55% gate → 不应注册。"""
    json_path = _write_payload(tmp_path, _make_payload(train_wr=0.50))

    catalog: "OrderedDict[str, SignalStrategy]" = OrderedDict()
    n = register_mined_rule_strategies(catalog, [json_path])

    assert n == 0
    assert len(catalog) == 0


def test_register_promote_only_false_loads_unfiltered(tmp_path: Path) -> None:
    """promote_only=False 时绕过 PROMOTION_GATES（用于研究审视）。"""
    json_path = _write_payload(tmp_path, _make_payload(train_wr=0.50))

    catalog: "OrderedDict[str, SignalStrategy]" = OrderedDict()
    n = register_mined_rule_strategies(catalog, [json_path], promote_only=False)

    assert n == 1


def test_register_does_not_overwrite_existing(tmp_path: Path) -> None:
    """已存在的同名 strategy 不被覆盖（避免意外替换手工策略）。"""
    json_path = _write_payload(tmp_path, _make_payload())

    catalog: "OrderedDict[str, SignalStrategy]" = OrderedDict()

    class _Dummy:
        name = "structured_mined_h1_buy_0"

    catalog["structured_mined_h1_buy_0"] = _Dummy()  # type: ignore[assignment]
    n = register_mined_rule_strategies(catalog, [json_path])

    assert n == 0  # 不覆盖
    assert isinstance(catalog["structured_mined_h1_buy_0"], _Dummy)


def test_register_handles_multiple_sources(tmp_path: Path) -> None:
    p1 = _write_payload(tmp_path, _make_payload(), name="m1.json")
    p2_payload = _make_payload()
    # p2 多一个 rule 改 tf 避免 name 冲突
    p2_payload["results"][0]["tf"] = "M30"
    p2 = _write_payload(tmp_path, p2_payload, name="m2.json")

    catalog: "OrderedDict[str, SignalStrategy]" = OrderedDict()
    n = register_mined_rule_strategies(catalog, [p1, p2])

    assert n == 2
    tfs = {s.name.split("_")[2] for s in catalog.values()}
    assert {"h1", "m30"}.issubset(tfs)


def test_register_returns_zero_on_missing_path(tmp_path: Path) -> None:
    """不存在的 path 不应 raise，返回 0（便于多 source 容错）。"""
    catalog: "OrderedDict[str, SignalStrategy]" = OrderedDict()
    n = register_mined_rule_strategies(catalog, [tmp_path / "does_not_exist.json"])
    assert n == 0
