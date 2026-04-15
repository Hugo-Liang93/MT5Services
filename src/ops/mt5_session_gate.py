from __future__ import annotations

from typing import Any


def probe_mt5_session_gate(
    *,
    instance_name: str | None = None,
    auto_launch_terminal: bool = True,
):
    """探测 MT5 session 是否可用。

    auto_launch_terminal=True（默认）：利用 MT5 库 `mt5.initialize(path=...)` 的自带能力，
    如果 terminal 进程未运行，由 initialize 自动拉起。这符合"启动服务 = 自动启动依赖"
    的运维直觉，不需要用户先手动打开 MT5 terminal。

    auto_launch_terminal=False：严格模式，仅检查已运行的 terminal 会话。
    适用于 dry-run preflight / runtime health 等不应有副作用的诊断场景。
    """
    from src.clients.base import MT5BaseClient
    from src.config.mt5 import load_mt5_settings

    mt5_cfg = load_mt5_settings(instance_name=instance_name)
    client = MT5BaseClient(settings=mt5_cfg)
    state = client.inspect_session_state(
        require_terminal_process=not auto_launch_terminal,
        attempt_initialize=True,
        attempt_login=True,
        shutdown_after_probe=True,
    )
    return mt5_cfg, state


def ensure_mt5_session_gate_or_raise(
    *,
    instance_name: str | None = None,
    auto_launch_terminal: bool = True,
):
    """启动前置 gate。默认允许自动拉起 terminal（见 probe_mt5_session_gate 注释）。"""
    mt5_cfg, state = probe_mt5_session_gate(
        instance_name=instance_name,
        auto_launch_terminal=auto_launch_terminal,
    )
    if not state.session_ready:
        instance_label = instance_name or mt5_cfg.instance_name or "default"
        raise RuntimeError(
            f"MT5 session gate failed for {instance_label} "
            f"[{state.error_code or 'unknown'}]: {state.error_message or 'unknown error'}"
        )
    return mt5_cfg, state


def ensure_topology_group_mt5_session_gate_or_raise(
    group_name: str,
) -> dict[str, dict[str, Any]]:
    from src.config.topology import load_topology_group

    group = load_topology_group(group_name)
    results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    for instance_name in [group.main, *group.workers]:
        try:
            settings, state = ensure_mt5_session_gate_or_raise(
                instance_name=instance_name
            )
            results[instance_name] = {
                "account_alias": settings.account_alias,
                "state": state.to_dict(),
            }
        except RuntimeError as exc:
            failures.append(str(exc))

    if failures:
        raise RuntimeError("; ".join(failures))

    return results
