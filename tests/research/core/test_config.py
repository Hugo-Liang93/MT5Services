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

import pytest

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
        assert (
            max(cfg.forward_horizons) >= 30
        ), "forward_horizons 最大值应 ≥ 30 以覆盖 Chandelier trailing 平均持仓。"

    def test_default_entry_meta_model_config_exists(self) -> None:
        cfg = ResearchConfig()

        assert cfg.entry_meta_model.model_kind == "tabular"
        assert cfg.entry_meta_model.top_bucket_quantile == 0.80
        assert cfg.entry_meta_model.min_samples == 40
        assert cfg.entry_meta_model.min_oos_samples == 10
        assert cfg.entry_meta_model.min_class_samples == 10
        assert cfg.entry_meta_model.threshold_grid == [0.50, 0.55, 0.60, 0.65, 0.70]
        assert cfg.entry_meta_model.feature_scope == "runtime_safe"


class TestPermutationBudget:
    def test_pp_n_permutations_default_500_not_1000(self) -> None:
        """2026-04-23 综合审查：pp n_permutations 1000→500 是故意的。

        BH-FDR 校正后 p-value 分辨率从 1e-3→2e-3，对显著性判定无实际影响。
        若将来有人改回 1000 请先读 docs/codebase-review.md §F 2026-04-23 综合审查。
        """
        cfg = ResearchConfig()
        assert (
            cfg.predictive_power.n_permutations == 500
        ), "pp n_permutations 应为 500（综合审查决定）。改回 1000 前请读审查记录。"


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

    def test_state_edge_config_overrides_applied(self, tmp_path: Path) -> None:
        """State Edge / GPU backend 配置从 research.ini 正确读入。"""
        ini = tmp_path / "research.ini"
        ini.write_text(
            textwrap.dedent(
                """
                [state_edge_model]
                enabled = true
                timeframes = H4,H1
                model_kind = hist_gradient_boosting
                label_policy = best_cost_after_barrier
                min_edge_return = 0.001
                top_bucket_quantile = 0.90
                threshold_grid = 0.60,0.70
                sequence_window = 32
                sequence_epochs = 3
                sequence_batch_size = 128
                sequence_learning_rate = 0.002
                min_oos_samples = 55
                min_top_bucket_samples = 7
                min_label_class_samples = 11

                [gpu_backend]
                preferred = gpu
                fail_fast = true
                readiness_benchmark_rows = 12345
                """
            ).strip(),
            encoding="utf-8",
        )

        cfg = load_research_config(ini)

        assert cfg.state_edge_model.enabled is True
        assert cfg.state_edge_model.timeframes == ["H4", "H1"]
        assert cfg.state_edge_model.min_edge_return == 0.001
        assert cfg.state_edge_model.top_bucket_quantile == 0.90
        assert cfg.state_edge_model.threshold_grid == [0.60, 0.70]
        assert cfg.state_edge_model.sequence_window == 32
        assert cfg.state_edge_model.sequence_epochs == 3
        assert cfg.state_edge_model.sequence_batch_size == 128
        assert cfg.state_edge_model.sequence_learning_rate == 0.002
        assert cfg.state_edge_model.min_oos_samples == 55
        assert cfg.state_edge_model.min_top_bucket_samples == 7
        assert cfg.state_edge_model.min_label_class_samples == 11
        assert cfg.gpu_backend.preferred == "gpu"
        assert cfg.gpu_backend.fail_fast is True
        assert cfg.gpu_backend.readiness_benchmark_rows == 12345

    def test_entry_meta_model_config_overrides_applied(self, tmp_path: Path) -> None:
        """Entry Meta model 配置从 research.ini 正确读入。"""
        ini = tmp_path / "research.ini"
        ini.write_text(
            textwrap.dedent(
                """
                [entry_meta_model]
                model_kind = tabular
                top_bucket_quantile = 0.85
                min_samples = 41
                min_oos_samples = 12
                min_class_samples = 13
                threshold_grid = 0.45,0.50,0.75
                feature_scope = research_full
                """
            ).strip(),
            encoding="utf-8",
        )

        cfg = load_research_config(ini)

        assert cfg.entry_meta_model.model_kind == "tabular"
        assert cfg.entry_meta_model.top_bucket_quantile == 0.85
        assert cfg.entry_meta_model.min_samples == 41
        assert cfg.entry_meta_model.min_oos_samples == 12
        assert cfg.entry_meta_model.min_class_samples == 13
        assert cfg.entry_meta_model.threshold_grid == [0.45, 0.50, 0.75]
        assert cfg.entry_meta_model.feature_scope == "research_full"

    def test_entry_meta_model_rejects_unknown_feature_scope(
        self, tmp_path: Path
    ) -> None:
        ini = tmp_path / "research.ini"
        ini.write_text(
            textwrap.dedent(
                """
                [entry_meta_model]
                feature_scope = invalid_scope
                """
            ).strip(),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="feature_scope"):
            load_research_config(ini)

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
        assert cfg.entry_meta_model == default_cfg.entry_meta_model


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
        assert (
            "- cost" in src or "-cost" in src
        ), "build_data_matrix 必须从 raw_return 扣除 cost，否则 cost 配置无效。"


class TestDefaultResearchConfigPath:
    """守护 _CONFIG_DIR 默认路径指向仓库根 config/。

    2026-04-27 评估发现：旧实现 `os.path.dirname` ×3 + `__file__` 实际指向
    `src/config`（不存在 research.ini），configparser 静默失败 →
    所有历史 mining 跑的都是 Pydantic 默认值，不是 config/research.ini 的口径。
    此测试族锁定修复，禁止回归。
    """

    def test_default_config_dir_points_to_repo_config(self) -> None:
        """默认 research 配置目录必须是仓库根目录 config/。"""
        from src.research.core import config as research_config_module

        expected = Path(__file__).resolve().parents[3] / "config"
        actual = Path(research_config_module._CONFIG_DIR)

        assert actual == expected
        assert (actual / "research.ini").exists()
        assert actual.name == "config"
        assert (actual.parent / "src").exists()

    def test_default_loader_uses_repo_research_ini(self) -> None:
        """默认 loader 应读取 config/research.ini 中的显式统计口径。"""
        cfg = load_research_config()

        assert cfg.overfitting.correction_method == "bh_fdr"
        assert cfg.feature_providers.fdr_grouping == "by_provider"

    def test_default_loader_merges_local_after_base(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """research.local.ini 必须覆盖 research.ini。"""
        from src.research.core import config as research_config_module

        (tmp_path / "research.ini").write_text(
            textwrap.dedent(
                """
                [overfitting]
                correction_method = bh_fdr
                [feature_providers]
                fdr_grouping = by_provider
                """
            ).strip(),
            encoding="utf-8",
        )
        (tmp_path / "research.local.ini").write_text(
            textwrap.dedent(
                """
                [overfitting]
                correction_method = bonferroni
                [feature_providers]
                fdr_grouping = global
                """
            ).strip(),
            encoding="utf-8",
        )

        monkeypatch.setattr(research_config_module, "_CONFIG_DIR", str(tmp_path))

        cfg = load_research_config()

        assert cfg.overfitting.correction_method == "bonferroni"
        assert cfg.feature_providers.fdr_grouping == "global"
