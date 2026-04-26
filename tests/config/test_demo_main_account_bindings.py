"""§0dd P2 回归：signal.local.ini account_bindings.* 必须与对应环境实际
装配的策略集合对齐——禁止已绑定但 candidate（不会装配）的策略，也禁止
demo_validation 但漏绑（demo 不能下单评估）的策略。

注：signal.local.ini 是 gitignored 的本机配置（CLAUDE.md 隐私分层）。
本 sentinel 在 signal.local.ini 缺失或 binding 全空时 skip——只在已配置
binding 的本机/环境上强制对齐，避免 CI 永远失败。
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pytest


def _load_merged_signal_config() -> tuple[configparser.ConfigParser, bool]:
    """合并 signal.ini + signal.local.ini，返 (parser, has_local_overrides)。"""
    repo_root = Path(__file__).resolve().parents[2]
    parser = configparser.ConfigParser()
    local_path = repo_root / "config" / "signal.local.ini"
    parser.read(
        [
            repo_root / "config" / "signal.ini",
            local_path,
        ],
        encoding="utf-8",
    )
    has_local = local_path.exists()
    return parser, has_local


def _strategies_with_status(
    parser: configparser.ConfigParser, status: str
) -> set[str]:
    out: set[str] = set()
    for section in parser.sections():
        if not section.startswith("strategy_deployment."):
            continue
        if not parser.has_option(section, "status"):
            continue
        if parser.get(section, "status").strip().lower() == status:
            name = section[len("strategy_deployment."):]
            out.add(name)
    return out


def _account_binding(
    parser: configparser.ConfigParser, alias: str
) -> set[str]:
    section = f"account_bindings.{alias}"
    if not parser.has_section(section):
        return set()
    raw = parser.get(section, "strategies", fallback="") or ""
    return {item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()}


def test_demo_main_binding_matches_demo_validation_deployment_set() -> None:
    """P2 §0dd 回归：demo_main binding 必须 == demo_validation 状态的策略集合。
    旧 binding 漂移：
    - 漏绑（demo 装但 binding 缺）→ demo 不能真实下单验证
    - 误绑（candidate 不装但 binding 有）→ 引用永不装配的策略
    """
    parser, has_local = _load_merged_signal_config()
    demo_main_binding = _account_binding(parser, "demo_main")
    if not has_local or not demo_main_binding:
        pytest.skip(
            "signal.local.ini 缺失或 demo_main binding 为空——CI 环境无本机"
            " binding override，跳过对齐检查；本机配置时该测试强制 binding 同步"
        )
    demo_validation = _strategies_with_status(parser, "demo_validation")

    missing_in_binding = demo_validation - demo_main_binding
    extra_in_binding = demo_main_binding - demo_validation

    assert not missing_in_binding, (
        f"demo_main binding 漏绑 demo_validation 策略 → demo 不能下单验证；"
        f"missing={sorted(missing_in_binding)!r}"
    )
    assert not extra_in_binding, (
        f"demo_main binding 引用非 demo_validation 策略 → 装配漂移；"
        f"extra={sorted(extra_in_binding)!r}"
    )


def test_live_main_binding_does_not_reference_unloadable_strategies() -> None:
    """对称契约：live_main binding 不能引用 candidate / demo_validation
    （live 环境只装 active/active_guarded）。允许空 binding（active=0 是当前
    状态事实，§0dd P2#2）。
    """
    parser, has_local = _load_merged_signal_config()
    live_main_binding = _account_binding(parser, "live_main")
    if not has_local:
        pytest.skip("signal.local.ini 缺失——CI 环境跳过 binding 对齐检查")
    live_executable = (
        _strategies_with_status(parser, "active")
        | _strategies_with_status(parser, "active_guarded")
    )
    extra = live_main_binding - live_executable
    assert not extra, (
        f"live_main binding 引用非 active/active_guarded 策略 → 装配漂移；"
        f"extra={sorted(extra)!r}"
    )
