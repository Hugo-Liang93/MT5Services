"""EntryPolicy 配置加载器。

Layered merge order（later overrides earlier）：
  1. config/entry_policy.ini
  2. config/entry_policy.local.ini
  3. config/instances/<instance>/entry_policy.ini（instance-scoped 时）
  4. config/instances/<instance>/entry_policy.local.ini（instance-scoped 时）

调用方式：from src.config import get_entry_policy_config
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, Tuple

from src.config.models.entry_policy import EntryPolicyConfig, TieBreakStrategy
from src.config.utils import get_merged_config

logger = logging.getLogger(__name__)


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _coerce_value(raw: str) -> Any:
    """ini 字符串值转 Python 类型：bool / int / float / list / 字面量 fallback。"""
    text = str(raw).strip()
    if text == "":
        return ""
    lower = text.lower()
    if lower in ("true", "yes", "on"):
        return True
    if lower in ("false", "no", "off"):
        return False
    if "," in text:
        items = [item.strip() for item in text.split(",") if item.strip()]
        try:
            return [float(item) if "." in item else int(item) for item in items]
        except ValueError:
            return items
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _build_strategy_mapping(section: Dict[str, Any]) -> Dict[str, str]:
    return {
        str(strategy).strip(): str(policy).strip()
        for strategy, policy in section.items()
        if str(strategy).strip() and str(policy).strip()
    }


def _build_strategy_tf_mapping(
    section: Dict[str, Any],
) -> Dict[Tuple[str, str], str]:
    """[mapping_per_tf] 键格式: strategy_name.tf"""
    result: Dict[Tuple[str, str], str] = {}
    for raw_key, raw_value in section.items():
        key = str(raw_key).strip()
        policy = str(raw_value).strip()
        if not key or not policy or "." not in key:
            continue
        strategy, tf = key.rsplit(".", 1)
        result[(strategy.strip(), tf.strip().upper())] = policy
    return result


def _build_policy_params(
    merged: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[Tuple[str, str], Dict[str, Any]]]:
    """从 merged 中收集 [policy_params.<name>] 与 [policy_params_per_tf.<name>.<tf>]。"""
    base: Dict[str, Dict[str, Any]] = {}
    per_tf: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for section_name, section_payload in merged.items():
        if section_name.startswith("policy_params_per_tf."):
            suffix = section_name[len("policy_params_per_tf.") :]
            if "." not in suffix:
                continue
            policy_name, tf = suffix.rsplit(".", 1)
            policy_name = policy_name.strip()
            tf = tf.strip().upper()
            payload = {
                str(k).strip(): _coerce_value(v) for k, v in section_payload.items()
            }
            per_tf[(policy_name, tf)] = payload
        elif section_name.startswith("policy_params."):
            policy_name = section_name[len("policy_params.") :].strip()
            if not policy_name:
                continue
            payload = {
                str(k).strip(): _coerce_value(v) for k, v in section_payload.items()
            }
            base[policy_name] = payload

    return base, per_tf


@lru_cache
def get_entry_policy_config() -> EntryPolicyConfig:
    """读取并组合 entry_policy.ini → EntryPolicyConfig。

    缓存策略与其他 config loader 一致；热重载场景由调用方调
    `reset_entry_policy_config_cache()`。
    """
    merged = get_merged_config("entry_policy.ini")

    registry_section = dict(merged.get("registry", {}))
    enabled_policies = _split_csv(registry_section.get("enabled", "market"))

    default_section = dict(merged.get("default", {}))
    default_policy = str(default_section.get("policy", "market")).strip() or "market"

    mapping_section = dict(merged.get("mapping", {}))
    strategy_mapping = _build_strategy_mapping(mapping_section)

    mapping_per_tf_section = dict(merged.get("mapping_per_tf", {}))
    strategy_tf_mapping = _build_strategy_tf_mapping(mapping_per_tf_section)

    policy_params, policy_tf_params = _build_policy_params(merged)

    fill_section = dict(merged.get("fill_semantics", {}))
    tie_break_raw = str(fill_section.get("tie_break", "limit_first")).strip().lower()
    valid_tie_breaks: tuple[TieBreakStrategy, ...] = (
        "limit_first",
        "stop_first",
        "alpha",
    )
    if tie_break_raw not in valid_tie_breaks:
        raise ValueError(
            f"[fill_semantics] tie_break={tie_break_raw!r} invalid; "
            f"expected one of {valid_tie_breaks}"
        )

    payload: Dict[str, Any] = {
        "enabled_policies": enabled_policies,
        "default_policy": default_policy,
        "strategy_mapping": strategy_mapping,
        "strategy_tf_mapping": strategy_tf_mapping,
        "policy_params": policy_params,
        "policy_tf_params": policy_tf_params,
        "fill_semantics_tie_break": tie_break_raw,
    }

    config = EntryPolicyConfig(**payload)
    logger.info(
        "EntryPolicyConfig loaded: enabled=%s default=%s mappings=%d tf_overrides=%d",
        config.enabled_policies,
        config.default_policy,
        len(config.strategy_mapping),
        len(config.strategy_tf_mapping),
    )
    return config


def reset_entry_policy_config_cache() -> None:
    """热重载用。"""
    get_entry_policy_config.cache_clear()


__all__ = [
    "get_entry_policy_config",
    "reset_entry_policy_config_cache",
]
