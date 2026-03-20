from __future__ import annotations

import json

from src.signals.strategies.registry import build_composite_strategies


def test_build_composite_strategies_loads_phase2_json_composites(tmp_path) -> None:
    config_path = tmp_path / "composites.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "breakout_release_confirm",
                    "sub_strategies": [
                        "DonchianBreakoutStrategy",
                        "SqueezeReleaseFollow",
                    ],
                    "combine_mode": "all_agree",
                    "regime_affinity": {
                        "TRENDING": 0.35,
                        "RANGING": 0.15,
                        "BREAKOUT": 1.0,
                        "UNCERTAIN": 0.5,
                    },
                    "preferred_scopes": ["confirmed"],
                },
                {
                    "name": "reversal_rejection_confirm",
                    "sub_strategies": [
                        "FakeBreakoutDetector",
                        "PriceActionReversal",
                    ],
                    "combine_mode": "all_agree",
                    "regime_affinity": {
                        "TRENDING": 0.10,
                        "RANGING": 1.0,
                        "BREAKOUT": 0.45,
                        "UNCERTAIN": 0.6,
                    },
                    "preferred_scopes": ["confirmed"],
                },
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    strategies = build_composite_strategies(config_path=str(config_path))
    descriptions = {item["name"]: item for item in (s.describe() for s in strategies)}

    assert "breakout_release_confirm" in descriptions
    assert descriptions["breakout_release_confirm"]["sub_strategies"] == [
        "donchian_breakout",
        "squeeze_release",
    ]
    assert "donchian20" in descriptions["breakout_release_confirm"][
        "required_indicators"
    ]
    assert "macd" in descriptions["breakout_release_confirm"]["required_indicators"]

    assert "reversal_rejection_confirm" in descriptions
    assert descriptions["reversal_rejection_confirm"]["sub_strategies"] == [
        "fake_breakout",
        "price_action_reversal",
    ]
    assert "atr14" in descriptions["reversal_rejection_confirm"]["required_indicators"]
