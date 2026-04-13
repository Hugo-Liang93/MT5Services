from __future__ import annotations

from typing import Any


def probe_mt5_session_gate(
    *,
    instance_name: str | None = None,
):
    from src.clients.base import MT5BaseClient
    from src.config.mt5 import load_mt5_settings

    mt5_cfg = load_mt5_settings(instance_name=instance_name)
    client = MT5BaseClient(settings=mt5_cfg)
    state = client.inspect_session_state(
        require_terminal_process=True,
        attempt_initialize=True,
        attempt_login=True,
        shutdown_after_probe=True,
    )
    return mt5_cfg, state


def ensure_mt5_session_gate_or_raise(
    *,
    instance_name: str | None = None,
):
    mt5_cfg, state = probe_mt5_session_gate(instance_name=instance_name)
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
