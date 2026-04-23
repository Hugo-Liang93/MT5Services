"""ResearchConfig 契约测试。

守护 2026-04-22 Gap 1/2a 调整不被无意回滚：
  - `round_trip_cost_pct` 默认 0.08（真实 XAUUSD 往返成本下限）
  - `forward_horizons` 默认 [3, 10, 30, 60]（覆盖 Chandelier 持仓期望，
    避免短 FR 与 exit 模型错配——见 docs/research-system.md 血教训段）

这两个值的漂移会让挖掘候选和实盘胜率系统性脱钩，必须测试守护。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from src.research.core.config import ResearchConfig, load_research_config


class TestResearchConfigDefaults:
    def test_default_round_trip_cost_pct_reflects_real_xauusd(self) -> None:
        """默认往返成本 ≥ 0.07%——XAUUSD 真实 spread+commission+滑点下限。"""
        cfg = ResearchConfig()
        assert cfg.round_trip_cost_pct >= 0.07, (
            "round_trip_cost_pct 默认值过低会高估候选预测力。"
            "真实 XAUUSD 往返成本 ≈ 0.07-0.10%。"
        )

    def test_default_forward_horizons_exclude_short_fr(self) -> None:
        """默认 forward_horizons 不含 1/5 bar 短 FR（与实盘 trailing 模型错配）。"""
        cfg = ResearchConfig()
        assert 1 not in cfg.forward_horizons
        assert 5 not in cfg.forward_horizons
        # 最大 horizon 应覆盖 Chandelier 期望持仓（20+ bar）
        assert max(cfg.forward_horizons) >= 30, (
            "forward_horizons 最大值应 ≥ 30 以覆盖 Chandelier trailing 平均持仓。"
        )


class TestPermutationBudget:
    def test_pp_n_permutations_default_500_not_1000(self) -> None:
        """2026-04-23 综合审查：pp n_permutations 1000→500 是故意的。

        BH-FDR 校正后 p-value 分辨率从 1e-3→2e-3，对显著性判定无实际影响。
        若将来有人改回 1000 请先读 docs/codebase-review.md §F 2026-04-23 综合审查。
        """
        cfg = ResearchConfig()
        assert cfg.predictive_power.n_permutations == 500, (
            "pp n_permutations 应为 500（综合审查决定）。改回 1000 前请读审查记录。"
        )


class TestLoadResearchConfigFromIni:
    def test_ini_overrides_applied(self, tmp_path: Path) -> None:
        """.ini 中的 forward_horizons / round_trip_cost_pct 正确读入。"""
        ini = tmp_path / "research.ini"
        ini.write_text(
            textwrap.dedent(
                """
                [research]
                forward_horizons = 5,20,40
                warmup_bars = 150
                train_ratio = 0.60
                round_trip_cost_pct = 0.12
                """
            ).strip(),
            encoding="utf-8",
        )

        cfg = load_research_config(ini)
        assert cfg.forward_horizons == [5, 20, 40]
        assert cfg.warmup_bars == 150
        assert cfg.train_ratio == 0.60
        assert cfg.round_trip_cost_pct == 0.12

    def test_missing_ini_falls_back_to_dataclass_defaults(self, tmp_path: Path) -> None:
        """缺失 .ini 时 loader fallback 必须与 dataclass 默认完全一致。"""
        empty_ini = tmp_path / "empty.ini"
        empty_ini.write_text("[research]\n", encoding="utf-8")

        cfg = load_research_config(empty_ini)
        default_cfg = ResearchConfig()

        assert cfg.forward_horizons == default_cfg.forward_horizons
        assert cfg.round_trip_cost_pct == default_cfg.round_trip_cost_pct
        assert cfg.warmup_bars == default_cfg.warmup_bars
        assert cfg.train_ratio == default_cfg.train_ratio


class TestCostReflectedInForwardReturns:
    """验证 round_trip_cost_pct 真的被 build_data_matrix 扣除到 forward_return。"""

    def test_cost_deduction_in_forward_return(self) -> None:
        """Cost 从 raw_return 扣除，IC 计算才会反映真实交易表现。"""
        # 这里做轻量语义验证：data_matrix.py build 流程里
        # `returns.append(raw_return - cost)` 是 cost 注入的唯一路径。
        # 若该路径被改，ResearchConfig.round_trip_cost_pct 不再影响 IC。
        import inspect

        from src.research.core import data_matrix

        src = inspect.getsource(data_matrix.build_data_matrix)
        assert "round_trip_cost_pct" in src
        assert "- cost" in src or "-cost" in src, (
            "build_data_matrix 必须从 raw_return 扣除 cost，否则 cost 配置无效。"
        )
