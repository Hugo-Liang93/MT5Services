"""研究模块配置加载 — 从 config/research.ini + research.local.ini 读取。"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config"
)


@dataclass(frozen=True)
class OverfittingConfig:
    min_samples: int = 30
    min_correlation: float = 0.05
    min_hit_rate_deviation: float = 0.03
    cv_folds: int = 5
    cv_consistency_threshold: float = 0.60
    correction_method: str = "bh_fdr"  # "bonferroni" | "bh_fdr"
    cv_mode: str = "expanding"  # "expanding" | "sliding"


@dataclass(frozen=True)
class RuleMiningConfig:
    """规则挖掘配置（从 config.py 统一管理）。"""

    max_depth: int = 3
    min_samples_leaf: int = 30
    min_hit_rate: float = 0.55
    min_test_hit_rate: float = 0.52
    max_rules: int = 20
    dimensionless_only: bool = True
    n_permutations: int = 200
    permutation_significance: float = 0.05
    cv_folds: int = 5
    cv_consistency_threshold: float = 0.40


@dataclass(frozen=True)
class ThresholdSweepConfig:
    sweep_points: int = 50
    target_metric: str = "expectancy"
    per_regime: bool = True
    n_permutations: int = 200
    permutation_significance: float = 0.05


@dataclass(frozen=True)
class PredictivePowerConfig:
    significance_level: float = 0.05
    per_regime: bool = True
    rolling_ic_enabled: bool = True
    rolling_ic_window: int = 60
    min_ir_threshold: float = 0.5
    permutation_test_enabled: bool = True
    n_permutations: int = 1000
    # 并行进程数：1 = 串行；>1 = multiprocessing。典型值为 cpu_count - 1
    permutation_workers: int = 1


@dataclass(frozen=True)
class FeatureEngineeringConfig:
    enabled: bool = True
    features: Optional[List[str]] = None  # None = 全部内置特征


@dataclass(frozen=True)
class TemporalProviderConfig:
    core_indicators: List[str] = field(default_factory=lambda: ["rsi14", "adx14"])
    aux_indicators: List[str] = field(
        default_factory=lambda: ["macd_histogram", "cci20", "roc12", "stoch_k"]
    )
    windows: List[int] = field(default_factory=lambda: [3, 5, 10])
    cross_levels_rsi: List[float] = field(default_factory=lambda: [30.0, 50.0, 70.0])
    cross_levels_adx: List[float] = field(default_factory=lambda: [20.0, 25.0])


@dataclass(frozen=True)
class MicrostructureProviderConfig:
    lookback: int = 5


@dataclass(frozen=True)
class CrossTFProviderConfig:
    parent_tf_map: Dict[str, str] = field(
        default_factory=lambda: {"M15": "H1", "M30": "H4", "H1": "H4", "H4": "D1"}
    )
    parent_indicators: List[str] = field(
        default_factory=lambda: [
            "supertrend_direction",
            "rsi14",
            "adx14",
            "ema50",
            "bb_position",
        ]
    )


@dataclass(frozen=True)
class RegimeTransitionProviderConfig:
    history_window: int = 50
    prob_delta_window: int = 5


@dataclass(frozen=True)
class FeatureProviderConfig:
    """所有 Provider 的启用开关和子配置。"""

    temporal_enabled: bool = True
    microstructure_enabled: bool = True
    cross_tf_enabled: bool = False  # 默认关闭（需额外加载父 TF 数据）
    regime_transition_enabled: bool = True
    session_event_enabled: bool = True
    intrabar_enabled: bool = True
    fdr_grouping: str = "by_provider"  # "by_provider" | "global" | "none"

    temporal: TemporalProviderConfig = field(default_factory=TemporalProviderConfig)
    microstructure: MicrostructureProviderConfig = field(
        default_factory=MicrostructureProviderConfig
    )
    cross_tf: CrossTFProviderConfig = field(default_factory=CrossTFProviderConfig)
    regime_transition: RegimeTransitionProviderConfig = field(
        default_factory=RegimeTransitionProviderConfig
    )

    def is_enabled(self, provider_name: str) -> bool:
        """查询指定 Provider 是否启用。未知 provider 名默认返回 True。"""
        attr = f"{provider_name}_enabled"
        return bool(getattr(self, attr, True))


@dataclass(frozen=True)
class ResearchConfig:
    forward_horizons: List[int] = field(default_factory=lambda: [1, 3, 5, 10])
    warmup_bars: int = 200
    train_ratio: float = 0.70
    # 单次往返交易成本（百分比），用于扣除 forward_return
    # XAUUSD 约 spread 3-5 pips + commission ≈ 0.03-0.05%
    round_trip_cost_pct: float = 0.04

    overfitting: OverfittingConfig = field(default_factory=OverfittingConfig)
    threshold_sweep: ThresholdSweepConfig = field(default_factory=ThresholdSweepConfig)
    predictive_power: PredictivePowerConfig = field(
        default_factory=PredictivePowerConfig
    )
    rule_mining: RuleMiningConfig = field(default_factory=RuleMiningConfig)
    feature_engineering: FeatureEngineeringConfig = field(
        default_factory=FeatureEngineeringConfig
    )
    feature_providers: FeatureProviderConfig = field(
        default_factory=FeatureProviderConfig
    )


def _load_feature_providers(parser: configparser.ConfigParser) -> FeatureProviderConfig:
    """从 INI 的 [feature_providers] 及子节解析 FeatureProviderConfig。"""

    def _get(section: str, key: str, fallback: str) -> str:
        return parser.get(section, key, fallback=fallback)

    def _getint(section: str, key: str, fallback: int) -> int:
        return parser.getint(section, key, fallback=fallback)

    def _getfloat(section: str, key: str, fallback: float) -> float:
        return parser.getfloat(section, key, fallback=fallback)

    def _getbool(section: str, key: str, fallback: bool) -> bool:
        return parser.getboolean(section, key, fallback=fallback)

    def _parse_str_list(raw: str) -> List[str]:
        return [v.strip() for v in raw.split(",") if v.strip()]

    def _parse_int_list(raw: str) -> List[int]:
        return [int(v.strip()) for v in raw.split(",") if v.strip()]

    def _parse_float_list(raw: str) -> List[float]:
        return [float(v.strip()) for v in raw.split(",") if v.strip()]

    def _parse_kv_map(raw: str) -> Dict[str, str]:
        """解析 'K1:V1,K2:V2' 格式为字典。"""
        result: Dict[str, str] = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" in pair:
                k, v = pair.split(":", 1)
                result[k.strip()] = v.strip()
        return result

    sec = "feature_providers"

    # --- [feature_providers.temporal] ---
    t_sec = "feature_providers.temporal"
    temporal = TemporalProviderConfig(
        core_indicators=_parse_str_list(
            _get(t_sec, "core_indicators", "rsi14,adx14")
        ),
        aux_indicators=_parse_str_list(
            _get(t_sec, "aux_indicators", "macd_histogram,cci20,roc12,stoch_k")
        ),
        windows=_parse_int_list(_get(t_sec, "windows", "3,5,10")),
        cross_levels_rsi=_parse_float_list(
            _get(t_sec, "cross_levels_rsi", "30,50,70")
        ),
        cross_levels_adx=_parse_float_list(_get(t_sec, "cross_levels_adx", "20,25")),
    )

    # --- [feature_providers.microstructure] ---
    m_sec = "feature_providers.microstructure"
    microstructure = MicrostructureProviderConfig(
        lookback=_getint(m_sec, "lookback", 5),
    )

    # --- [feature_providers.cross_tf] ---
    c_sec = "feature_providers.cross_tf"
    cross_tf = CrossTFProviderConfig(
        parent_tf_map=_parse_kv_map(
            _get(c_sec, "parent_tf_map", "M15:H1,M30:H4,H1:H4,H4:D1")
        ),
        parent_indicators=_parse_str_list(
            _get(
                c_sec,
                "parent_indicators",
                "supertrend_direction,rsi14,adx14,ema50,bb_position",
            )
        ),
    )

    # --- [feature_providers.regime_transition] ---
    r_sec = "feature_providers.regime_transition"
    regime_transition = RegimeTransitionProviderConfig(
        history_window=_getint(r_sec, "history_window", 50),
        prob_delta_window=_getint(r_sec, "prob_delta_window", 5),
    )

    return FeatureProviderConfig(
        temporal_enabled=_getbool(sec, "temporal", True),
        microstructure_enabled=_getbool(sec, "microstructure", True),
        cross_tf_enabled=_getbool(sec, "cross_tf", False),
        regime_transition_enabled=_getbool(sec, "regime_transition", True),
        session_event_enabled=_getbool(sec, "session_event", True),
        intrabar_enabled=_getbool(sec, "intrabar", True),
        fdr_grouping=_get(sec, "fdr_grouping", "by_provider"),
        temporal=temporal,
        microstructure=microstructure,
        cross_tf=cross_tf,
        regime_transition=regime_transition,
    )


def load_research_config() -> ResearchConfig:
    """加载研究模块配置，支持 research.local.ini 覆盖。"""
    parser = configparser.ConfigParser()
    ini_path = os.path.join(_CONFIG_DIR, "research.ini")
    local_path = os.path.join(_CONFIG_DIR, "research.local.ini")
    parser.read([ini_path, local_path], encoding="utf-8")

    def _get(section: str, key: str, fallback: str) -> str:
        return parser.get(section, key, fallback=fallback)

    def _getint(section: str, key: str, fallback: int) -> int:
        return parser.getint(section, key, fallback=fallback)

    def _getfloat(section: str, key: str, fallback: float) -> float:
        return parser.getfloat(section, key, fallback=fallback)

    def _getbool(section: str, key: str, fallback: bool) -> bool:
        return parser.getboolean(section, key, fallback=fallback)

    horizons_str = _get("research", "forward_horizons", "1,3,5,10")
    forward_horizons = [int(h.strip()) for h in horizons_str.split(",") if h.strip()]

    # 向后兼容: bonferroni_correction=true → correction_method=bonferroni
    correction_method = _get("overfitting", "correction_method", "")
    if not correction_method:
        legacy_bonf = _getbool("overfitting", "bonferroni_correction", True)
        correction_method = "bonferroni" if legacy_bonf else "bh_fdr"

    # feature_engineering.features: 逗号分隔字符串 → Optional[List[str]]
    features_str = _get("feature_engineering", "features", "").strip()
    fe_features: Optional[List[str]] = None
    if features_str:
        fe_features = [f.strip() for f in features_str.split(",") if f.strip()]

    cv_mode = _get("overfitting", "cv_mode", "expanding")

    return ResearchConfig(
        forward_horizons=forward_horizons,
        warmup_bars=_getint("research", "warmup_bars", 200),
        train_ratio=_getfloat("research", "train_ratio", 0.70),
        round_trip_cost_pct=_getfloat("research", "round_trip_cost_pct", 0.04),
        overfitting=OverfittingConfig(
            min_samples=_getint("overfitting", "min_samples", 30),
            min_correlation=_getfloat("overfitting", "min_correlation", 0.05),
            min_hit_rate_deviation=_getfloat(
                "overfitting", "min_hit_rate_deviation", 0.03
            ),
            cv_folds=_getint("overfitting", "cv_folds", 5),
            cv_consistency_threshold=_getfloat(
                "overfitting", "cv_consistency_threshold", 0.60
            ),
            correction_method=correction_method,
            cv_mode=cv_mode,
        ),
        threshold_sweep=ThresholdSweepConfig(
            sweep_points=_getint("threshold_sweep", "sweep_points", 50),
            target_metric=_get("threshold_sweep", "target_metric", "expectancy"),
            per_regime=_getbool("threshold_sweep", "per_regime", True),
            n_permutations=_getint("threshold_sweep", "n_permutations", 200),
            permutation_significance=_getfloat(
                "threshold_sweep", "permutation_significance", 0.05
            ),
        ),
        predictive_power=PredictivePowerConfig(
            significance_level=_getfloat(
                "predictive_power", "significance_level", 0.05
            ),
            per_regime=_getbool("predictive_power", "per_regime", True),
            rolling_ic_enabled=_getbool("predictive_power", "rolling_ic_enabled", True),
            rolling_ic_window=_getint("predictive_power", "rolling_ic_window", 60),
            min_ir_threshold=_getfloat("predictive_power", "min_ir_threshold", 0.5),
            permutation_test_enabled=_getbool(
                "predictive_power", "permutation_test_enabled", True
            ),
            n_permutations=_getint("predictive_power", "n_permutations", 1000),
            permutation_workers=_getint("predictive_power", "permutation_workers", 1),
        ),
        rule_mining=RuleMiningConfig(
            max_depth=_getint("rule_mining", "max_depth", 3),
            min_samples_leaf=_getint("rule_mining", "min_samples_leaf", 30),
            min_hit_rate=_getfloat("rule_mining", "min_hit_rate", 0.55),
            min_test_hit_rate=_getfloat("rule_mining", "min_test_hit_rate", 0.52),
            max_rules=_getint("rule_mining", "max_rules", 20),
            dimensionless_only=_getbool("rule_mining", "dimensionless_only", True),
            n_permutations=_getint("rule_mining", "n_permutations", 200),
            permutation_significance=_getfloat(
                "rule_mining", "permutation_significance", 0.05
            ),
            cv_folds=_getint("rule_mining", "cv_folds", 5),
            cv_consistency_threshold=_getfloat(
                "rule_mining", "cv_consistency_threshold", 0.40
            ),
        ),
        feature_engineering=FeatureEngineeringConfig(
            enabled=_getbool("feature_engineering", "enabled", True),
            features=fe_features,
        ),
        feature_providers=_load_feature_providers(parser),
    )
