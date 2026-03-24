"""参数推荐引擎与配置应用器。

基于 Walk-Forward 验证结果自动生成策略参数和 Regime 亲和度的调优推荐，
经人工审核后一键应用到 signal.local.ini（不修改主配置文件）。

流程：WalkForwardResult → RecommendationEngine.generate() → Recommendation
      → 人工 approve → ConfigApplicator.apply() → signal.local.ini + 内存热更新
"""

from __future__ import annotations

import configparser
import logging
import shutil
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .models import (
    ParamChange,
    Recommendation,
    RecommendationStatus,
)

if TYPE_CHECKING:
    from src.signals.service import SignalModule

    from .walk_forward import WalkForwardResult

logger = logging.getLogger(__name__)

# ── 安全护栏常量 ─────────────────────────────────────────────────────
MAX_CHANGE_PCT = 30.0  # 单参数最大变更幅度（%）
MAX_CHANGES_PER_REC = 10  # 每次推荐最多参数变更数
MIN_OOS_TRADES = 30  # OOS 最小交易数
MAX_OVERFITTING_RATIO = 2.0  # 最大允许过拟合比
MIN_CONSISTENCY_RATE = 0.6  # 最低 OOS 窗口盈利比例

CONFIG_DIR = Path("config")
BACKUP_DIR = CONFIG_DIR / "backups"


class RecommendationEngine:
    """基于 Walk-Forward 验证结果生成参数推荐。

    算法：
    1. 安全门槛检查（过拟合/一致性/样本量）
    2. 多窗口 best_params 中位数聚合（抗过拟合）
    3. 与当前配置对比，计算变更幅度
    4. 变更幅度裁剪（±30%）
    5. 按 OOS 改善排序，取 top 10
    """

    def generate(
        self,
        wf_result: "WalkForwardResult",
        source_run_id: str,
        current_strategy_params: Optional[Dict[str, Any]] = None,
        current_regime_affinities: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> Recommendation:
        """从 Walk-Forward 结果生成参数推荐。

        Args:
            wf_result: Walk-Forward 验证结果
            source_run_id: 来源 run_id
            current_strategy_params: 当前生产 strategy_params（用于计算 diff）
            current_regime_affinities: 当前 regime_affinity 覆盖

        Returns:
            Recommendation 推荐记录

        Raises:
            ValueError: 不满足安全门槛时抛出
        """
        current_strategy_params = current_strategy_params or {}
        current_regime_affinities = current_regime_affinities or {}

        # ── 安全门槛检查 ──────────────────────────────────────────────
        self._validate_safety(wf_result)

        agg = wf_result.aggregate_metrics

        # ── 策略参数：多窗口中位数聚合 ────────────────────────────────
        strategy_changes = self._aggregate_strategy_params(
            wf_result, current_strategy_params
        )

        # ── Regime 亲和度推荐 ─────────────────────────────────────────
        affinity_changes = self._recommend_regime_affinities(
            wf_result, current_regime_affinities
        )

        # ── 合并、裁剪、排序 ─────────────────────────────────────────
        all_changes = strategy_changes + affinity_changes
        all_changes = self._clip_changes(all_changes)
        all_changes = self._rank_and_limit(all_changes)

        # ── 生成 rationale ────────────────────────────────────────────
        rationale = self._build_rationale(wf_result, all_changes)

        return Recommendation(
            rec_id=f"rec_{uuid.uuid4().hex[:12]}",
            source_run_id=source_run_id,
            created_at=datetime.now(timezone.utc),
            status=RecommendationStatus.PENDING,
            overfitting_ratio=wf_result.overfitting_ratio,
            consistency_rate=wf_result.consistency_rate,
            oos_sharpe=agg.sharpe_ratio,
            oos_win_rate=agg.win_rate,
            oos_total_trades=agg.total_trades,
            changes=all_changes,
            rationale=rationale,
        )

    def _validate_safety(self, wf_result: "WalkForwardResult") -> None:
        """检查 Walk-Forward 结果是否满足安全门槛。"""
        if wf_result.overfitting_ratio >= MAX_OVERFITTING_RATIO:
            raise ValueError(
                f"过拟合比过高: {wf_result.overfitting_ratio:.2f} "
                f"(阈值 < {MAX_OVERFITTING_RATIO})"
            )
        if wf_result.consistency_rate < MIN_CONSISTENCY_RATE:
            raise ValueError(
                f"OOS 一致性不足: {wf_result.consistency_rate:.2f} "
                f"(阈值 >= {MIN_CONSISTENCY_RATE})"
            )
        agg = wf_result.aggregate_metrics
        if agg.total_trades < MIN_OOS_TRADES:
            raise ValueError(
                f"OOS 交易样本不足: {agg.total_trades} " f"(阈值 >= {MIN_OOS_TRADES})"
            )

    def _aggregate_strategy_params(
        self,
        wf_result: "WalkForwardResult",
        current_params: Dict[str, Any],
    ) -> List[ParamChange]:
        """多窗口 best_params 中位数聚合，与当前值对比生成变更。"""
        # 收集每个参数在各窗口的值
        param_values: Dict[str, List[float]] = {}
        for split in wf_result.splits:
            for key, value in split.best_params.items():
                try:
                    fval = float(value)
                except (TypeError, ValueError):
                    continue
                # 只处理 strategy_params 格式的键（双下划线分隔）
                if "__" not in key:
                    continue
                param_values.setdefault(key, []).append(fval)

        changes: List[ParamChange] = []
        for key, values in param_values.items():
            if len(values) < 2:
                # 至少 2 个窗口才有统计意义
                continue
            median_val = statistics.median(values)
            old_val = current_params.get(key)
            old_float: Optional[float] = None
            if old_val is not None:
                try:
                    old_float = float(old_val)
                except (TypeError, ValueError):
                    continue

            if old_float is not None and old_float != 0:
                change_pct = (median_val - old_float) / abs(old_float) * 100
            elif old_float == 0:
                change_pct = 100.0 if median_val != 0 else 0.0
            else:
                change_pct = 0.0  # 新增参数

            # 跳过变更极小的参数（< 1%）
            if abs(change_pct) < 1.0 and old_float is not None:
                continue

            changes.append(
                ParamChange(
                    section="strategy_params",
                    key=key,
                    old_value=old_float,
                    new_value=median_val,
                    change_pct=change_pct,
                )
            )
        return changes

    def _recommend_regime_affinities(
        self,
        wf_result: "WalkForwardResult",
        current_affinities: Dict[str, Dict[str, float]],
    ) -> List[ParamChange]:
        """基于 OOS metrics_by_strategy × metrics_by_regime 推荐亲和度调整。"""
        changes: List[ParamChange] = []

        # 收集各窗口的 OOS metrics_by_strategy 中的胜率
        strategy_regime_wins: Dict[str, Dict[str, List[float]]] = {}
        for split in wf_result.splits:
            oos = split.out_of_sample_result
            # 遍历 metrics_by_strategy 提取每个策略的表现
            for strat_name, metrics in oos.metrics_by_strategy.items():
                if metrics.total_trades < 3:
                    continue
                # 取整体胜率作为近似
                strategy_regime_wins.setdefault(strat_name, {}).setdefault(
                    "_overall", []
                ).append(metrics.win_rate)

            # 遍历 metrics_by_regime 看各 regime 下的整体表现
            for regime_name, metrics in oos.metrics_by_regime.items():
                if metrics.total_trades < 3:
                    continue
                strategy_regime_wins.setdefault("_regime_perf", {}).setdefault(
                    regime_name, []
                ).append(metrics.win_rate)

        # 简化推荐：对已有覆盖的策略×regime 组合，
        # 基于 OOS 整体胜率给出方向性建议
        regime_keys = ["trending", "ranging", "breakout", "uncertain"]
        for strat_name, curr_affinity in current_affinities.items():
            strat_data = strategy_regime_wins.get(strat_name, {})
            overall_rates = strat_data.get("_overall", [])
            if len(overall_rates) < 2:
                continue
            avg_win_rate = statistics.mean(overall_rates)

            for regime_key in regime_keys:
                old_val = curr_affinity.get(regime_key)
                if old_val is None:
                    continue

                # 高胜率策略 → 适度提升亲和度，低胜率 → 降低
                if avg_win_rate > 55.0 and old_val < 0.8:
                    new_val = min(old_val * 1.15, 1.0)
                elif avg_win_rate < 40.0 and old_val > 0.2:
                    new_val = max(old_val * 0.85, 0.05)
                else:
                    continue

                if old_val != 0:
                    change_pct = (new_val - old_val) / abs(old_val) * 100
                else:
                    change_pct = 100.0

                if abs(change_pct) < 1.0:
                    continue

                changes.append(
                    ParamChange(
                        section=f"regime_affinity.{strat_name}",
                        key=regime_key,
                        old_value=old_val,
                        new_value=round(new_val, 4),
                        change_pct=change_pct,
                    )
                )

        return changes

    def _clip_changes(self, changes: List[ParamChange]) -> List[ParamChange]:
        """裁剪超出最大变更幅度的参数。"""
        clipped: List[ParamChange] = []
        for c in changes:
            if abs(c.change_pct) > MAX_CHANGE_PCT and c.old_value is not None:
                # 裁剪到最大变更幅度
                direction = 1.0 if c.change_pct > 0 else -1.0
                new_val = c.old_value * (1.0 + direction * MAX_CHANGE_PCT / 100.0)
                new_pct = direction * MAX_CHANGE_PCT
                clipped.append(
                    ParamChange(
                        section=c.section,
                        key=c.key,
                        old_value=c.old_value,
                        new_value=round(new_val, 6),
                        change_pct=round(new_pct, 2),
                    )
                )
            else:
                clipped.append(c)
        return clipped

    def _rank_and_limit(self, changes: List[ParamChange]) -> List[ParamChange]:
        """按变更幅度绝对值排序，取 top N。"""
        sorted_changes = sorted(changes, key=lambda c: abs(c.change_pct), reverse=True)
        return sorted_changes[:MAX_CHANGES_PER_REC]

    def _build_rationale(
        self,
        wf_result: "WalkForwardResult",
        changes: List[ParamChange],
    ) -> str:
        """生成推荐理由说明。"""
        agg = wf_result.aggregate_metrics
        parts = [
            f"Walk-Forward {len(wf_result.splits)} 窗口验证通过: "
            f"overfitting_ratio={wf_result.overfitting_ratio:.2f}, "
            f"OOS 一致性={wf_result.consistency_rate:.0%}",
            f"OOS 聚合指标: Sharpe={agg.sharpe_ratio:.2f}, "
            f"胜率={agg.win_rate:.1f}%, "
            f"交易数={agg.total_trades}, "
            f"最大回撤={agg.max_drawdown:.2f}%",
        ]

        strategy_changes = [c for c in changes if c.section == "strategy_params"]
        affinity_changes = [
            c for c in changes if c.section.startswith("regime_affinity")
        ]

        if strategy_changes:
            param_strs = [
                f"  {c.key}: {c.old_value} → {c.new_value:.4f} ({c.change_pct:+.1f}%)"
                for c in strategy_changes
            ]
            parts.append("策略参数变更（中位数聚合）:\n" + "\n".join(param_strs))

        if affinity_changes:
            aff_strs = [
                f"  [{c.section}] {c.key}: {c.old_value} → {c.new_value:.4f} "
                f"({c.change_pct:+.1f}%)"
                for c in affinity_changes
            ]
            parts.append("Regime 亲和度变更:\n" + "\n".join(aff_strs))

        return "\n".join(parts)


class ConfigApplicator:
    """将审核通过的参数推荐应用到配置文件和运行时内存。

    写入 signal.local.ini（优先级 > signal.ini），不修改主配置文件。
    """

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        signal_module: Optional["SignalModule"] = None,
    ) -> None:
        self._config_dir = config_dir or CONFIG_DIR
        self._signal_module = signal_module

    def apply(self, rec: Recommendation) -> str:
        """应用推荐的参数变更。

        1. 备份当前 signal.local.ini
        2. 将变更写入 signal.local.ini
        3. 内存热更新（如果 signal_module 可用）

        Args:
            rec: 已审核通过的推荐记录

        Returns:
            备份文件路径

        Raises:
            ValueError: 推荐状态不是 approved
        """
        if rec.status != RecommendationStatus.APPROVED:
            raise ValueError(
                f"推荐 {rec.rec_id} 状态为 {rec.status.value}，"
                "需要 approved 状态才能应用"
            )

        # ── 备份 ──────────────────────────────────────────────────────
        backup_path = self._backup_local_ini()

        # ── 写入 signal.local.ini ────────────────────────────────────
        self._write_local_ini(rec.changes)

        # ── 内存热更新 ───────────────────────────────────────────────
        if self._signal_module is not None:
            strategy_params, affinity_overrides = self._extract_override_dicts(
                rec.changes
            )
            self._signal_module.apply_param_overrides(
                strategy_params, affinity_overrides
            )
            logger.info(
                "内存热更新完成: %d 策略参数, %d 亲和度覆盖",
                len(strategy_params),
                len(affinity_overrides),
            )

        # ── 更新推荐状态 ─────────────────────────────────────────────
        rec.status = RecommendationStatus.APPLIED
        rec.applied_at = datetime.now(timezone.utc)
        rec.backup_path = str(backup_path)

        logger.info("推荐 %s 已应用，备份: %s", rec.rec_id, backup_path)
        return str(backup_path)

    def rollback(self, rec: Recommendation) -> None:
        """回滚已应用的推荐。

        从备份恢复 signal.local.ini 并重新加载内存参数。

        Raises:
            ValueError: 推荐状态不是 applied 或无备份路径
        """
        if rec.status != RecommendationStatus.APPLIED:
            raise ValueError(
                f"推荐 {rec.rec_id} 状态为 {rec.status.value}，"
                "需要 applied 状态才能回滚"
            )
        if not rec.backup_path:
            raise ValueError(f"推荐 {rec.rec_id} 没有备份路径")

        backup_path = Path(rec.backup_path)
        local_ini = self._config_dir / "signal.local.ini"

        if backup_path.exists():
            shutil.copy2(backup_path, local_ini)
            logger.info("配置已从 %s 恢复", backup_path)
        elif local_ini.exists():
            # 备份丢失但 local.ini 存在——删除我们写入的变更
            local_ini.unlink()
            logger.warning("备份 %s 不存在，已删除 signal.local.ini", backup_path)

        # 重新加载内存参数
        if self._signal_module is not None:
            restored_params, restored_affinities = self._read_current_overrides()
            self._signal_module.apply_param_overrides(
                restored_params, restored_affinities
            )

        rec.status = RecommendationStatus.ROLLED_BACK
        rec.rolled_back_at = datetime.now(timezone.utc)
        logger.info("推荐 %s 已回滚", rec.rec_id)

    def _backup_local_ini(self) -> Path:
        """备份当前 signal.local.ini（如果存在）。"""
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        local_ini = self._config_dir / "signal.local.ini"
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"signal.local.{ts}.ini"

        if local_ini.exists():
            shutil.copy2(local_ini, backup_path)
        else:
            # 创建空备份标记（回滚时知道原来没有 local.ini）
            backup_path.write_text("# empty - no prior signal.local.ini\n")

        return backup_path

    def _write_local_ini(self, changes: List[ParamChange]) -> None:
        """将变更合并写入 signal.local.ini。"""
        local_ini = self._config_dir / "signal.local.ini"
        parser = configparser.ConfigParser()

        # 读取已有的 local.ini（保留其他手动配置）
        if local_ini.exists():
            parser.read(str(local_ini))

        for change in changes:
            if change.section == "strategy_params":
                if not parser.has_section("strategy_params"):
                    parser.add_section("strategy_params")
                parser.set("strategy_params", change.key, str(change.new_value))
            elif change.section.startswith("regime_affinity."):
                section_name = change.section
                if not parser.has_section(section_name):
                    parser.add_section(section_name)
                parser.set(section_name, change.key, str(change.new_value))

        with open(local_ini, "w") as f:
            f.write(
                "# 由参数推荐系统自动生成，优先级高于 signal.ini\n"
                "# 手动编辑后需重启服务生效\n\n"
            )
            parser.write(f)

        logger.info("signal.local.ini 已更新: %d 项变更", len(changes))

    def _read_current_overrides(
        self,
    ) -> Tuple[Dict[str, Any], Dict[str, Dict[str, float]]]:
        """从 signal.local.ini 读取当前覆盖参数。"""
        local_ini = self._config_dir / "signal.local.ini"
        strategy_params: Dict[str, Any] = {}
        affinity_overrides: Dict[str, Dict[str, float]] = {}

        if not local_ini.exists():
            return strategy_params, affinity_overrides

        parser = configparser.ConfigParser()
        parser.read(str(local_ini))

        if parser.has_section("strategy_params"):
            for key, val in parser.items("strategy_params"):
                try:
                    strategy_params[key] = float(val)
                except ValueError:
                    strategy_params[key] = val

        for section in parser.sections():
            if section.startswith("regime_affinity."):
                strat_name = section.split(".", 1)[1]
                affinity_overrides[strat_name] = {}
                for key, val in parser.items(section):
                    try:
                        affinity_overrides[strat_name][key] = float(val)
                    except ValueError:
                        pass

        return strategy_params, affinity_overrides

    @staticmethod
    def _extract_override_dicts(
        changes: List[ParamChange],
    ) -> Tuple[Dict[str, Any], Dict[str, Dict[str, float]]]:
        """从 ParamChange 列表提取 apply_param_overrides 所需的字典。"""
        strategy_params: Dict[str, Any] = {}
        affinity_overrides: Dict[str, Dict[str, float]] = {}

        for c in changes:
            if c.section == "strategy_params":
                strategy_params[c.key] = c.new_value
            elif c.section.startswith("regime_affinity."):
                strat_name = c.section.split(".", 1)[1]
                affinity_overrides.setdefault(strat_name, {})[c.key] = c.new_value

        return strategy_params, affinity_overrides


def load_current_signal_config() -> Tuple[Dict[str, Any], Dict[str, Dict[str, float]]]:
    """从 signal.ini + signal.local.ini 加载当前策略参数和亲和度覆盖。

    Returns:
        (strategy_params, regime_affinity_overrides) 元组
    """
    from src.config.utils import get_merged_config

    merged = get_merged_config("signal.ini")

    strategy_params: Dict[str, Any] = {}
    affinity_overrides: Dict[str, Dict[str, float]] = {}

    sp = merged.get("strategy_params", {})
    for key, val in sp.items():
        try:
            strategy_params[key] = float(val)
        except (TypeError, ValueError):
            strategy_params[key] = val

    for section_name, section_data in merged.items():
        if section_name.startswith("regime_affinity."):
            strat_name = section_name.split(".", 1)[1]
            affinity_overrides[strat_name] = {}
            for key, val in section_data.items():
                try:
                    affinity_overrides[strat_name][key] = float(val)
                except (TypeError, ValueError):
                    pass

    return strategy_params, affinity_overrides
