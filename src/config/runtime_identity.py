from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from src.config.instance_context import (
    get_current_instance_name,
    normalize_environment,
    normalize_instance_name,
)
from src.config.mt5 import MT5Settings, load_group_mt5_settings, load_mt5_settings
from src.config.topology import resolve_current_environment, resolve_topology_assignment


def build_account_key(
    environment: str,
    mt5_server: str | None,
    login: int | str | None,
) -> str:
    normalized_environment = normalize_environment(environment)
    if normalized_environment is None:
        raise ValueError("environment is required to build account_key")
    normalized_server = str(mt5_server or "unknown-server").strip().lower() or "unknown-server"
    normalized_login = str(login or "unknown-login").strip() or "unknown-login"
    return f"{normalized_environment}:{normalized_server}:{normalized_login}"


def _normalized_terminal_path(path: str | None) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve()).lower()
    except OSError:
        return raw.lower()


def _default_assignment(
    instance_name: str | None,
    environment: str | None,
) -> tuple[str, str, str, str]:
    normalized = normalize_instance_name(instance_name)
    resolved_name = normalized or "default"
    resolved_environment = resolve_current_environment(environment)
    if resolved_environment is None:
        raise ValueError("unable to resolve runtime environment from topology")
    return resolved_name, resolved_environment, "main", "single_account"


def validate_mt5_topology() -> None:
    normalized_instance = normalize_instance_name(get_current_instance_name())
    assignment = resolve_topology_assignment(normalized_instance)

    if assignment is None:
        environment = resolve_current_environment(instance_name=normalized_instance)
        instance_name, environment, instance_role, live_topology_mode = _default_assignment(
            normalized_instance,
            environment,
        )
    else:
        instance_name = assignment.instance_name
        environment = assignment.environment
        instance_role = assignment.role
        live_topology_mode = assignment.live_topology_mode

    if environment == "demo":
        if live_topology_mode != "single_account":
            raise ValueError("demo 环境只支持 single_account 模式")
        if instance_role != "main":
            raise ValueError("demo 环境不支持 executor 实例角色")
        return

    if instance_role == "executor" and live_topology_mode != "multi_account":
        raise ValueError("executor 实例仅允许在 live multi_account 模式下运行")

    if live_topology_mode != "multi_account":
        return

    if environment != "live":
        raise ValueError("multi_account 模式仅允许在 live 环境下运行")

    seen_paths: dict[str, str] = {}
    for account in load_group_mt5_settings(instance_name=instance_name).values():
        normalized_path = _normalized_terminal_path(account.mt5_path)
        if not normalized_path:
            raise ValueError(
                f"live multi_account 模式要求账户 {account.account_alias} 配置唯一 terminal path"
            )
        existing_alias = seen_paths.get(normalized_path)
        if existing_alias is not None and existing_alias != account.account_alias:
            raise ValueError(
                "live multi_account 模式要求所有启用的 live 账户使用不同 terminal path: "
                f"{existing_alias} 与 {account.account_alias} 冲突"
            )
        seen_paths[normalized_path] = account.account_alias


@dataclass(frozen=True)
class RuntimeIdentity:
    instance_name: str
    environment: str
    instance_id: str
    instance_role: str
    live_topology_mode: str
    account_alias: str
    account_label: str | None
    account_key: str
    mt5_server: str | None
    mt5_login: int | None
    mt5_path: str | None


def _build_runtime_identity(account: MT5Settings) -> RuntimeIdentity:
    validate_mt5_topology()
    assignment = resolve_topology_assignment(account.instance_name)
    if assignment is None:
        environment = resolve_current_environment(instance_name=account.instance_name)
        instance_name, environment, instance_role, live_topology_mode = _default_assignment(
            account.instance_name,
            environment,
        )
    else:
        instance_name = assignment.instance_name
        environment = assignment.environment
        instance_role = assignment.role
        live_topology_mode = assignment.live_topology_mode

    return RuntimeIdentity(
        instance_name=instance_name,
        environment=environment,
        instance_id=f"{instance_role}-{instance_name}-{uuid4().hex[:12]}",
        instance_role=instance_role,
        live_topology_mode=live_topology_mode,
        account_alias=account.account_alias,
        account_label=account.account_label,
        account_key=build_account_key(
            environment,
            account.mt5_server,
            account.mt5_login,
        ),
        mt5_server=account.mt5_server,
        mt5_login=account.mt5_login,
        mt5_path=account.mt5_path,
    )


@lru_cache
def get_runtime_identity() -> RuntimeIdentity:
    account = load_mt5_settings()
    return _build_runtime_identity(account)


def list_mt5_accounts() -> list[MT5Settings]:
    normalized_instance = normalize_instance_name(get_current_instance_name())
    return list(load_group_mt5_settings(instance_name=normalized_instance).values())
