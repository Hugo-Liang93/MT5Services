from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.config.instance_context import (
    get_current_environment,
    get_current_instance_name,
    normalize_environment,
    normalize_instance_name,
)
from src.config.utils import load_ini_config


def _split_csv(value: str | None) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


@dataclass(frozen=True)
class TopologyGroup:
    name: str
    main: str
    workers: tuple[str, ...]

    @property
    def instances(self) -> tuple[str, ...]:
        return (self.main, *self.workers)

    @property
    def live_topology_mode(self) -> str:
        return "multi_account" if self.workers else "single_account"

    @property
    def environment(self) -> str:
        return self.name


@dataclass(frozen=True)
class TopologyAssignment:
    instance_name: str
    environment: str
    role: str
    live_topology_mode: str


def load_topology_groups(*, base_dir: str | None = None) -> dict[str, TopologyGroup]:
    path, parser = load_ini_config("topology.ini", base_dir=base_dir)
    if not path or parser is None:
        return {}

    groups: dict[str, TopologyGroup] = {}
    for section_name in parser.sections():
        if not section_name.startswith("group."):
            continue
        group_name = normalize_environment(section_name.split(".", 1)[1].strip())
        section = parser[section_name]
        main_instance = str(section.get("main", "")).strip()
        workers = tuple(_split_csv(section.get("workers", "")))
        if not group_name or not main_instance:
            continue
        groups[group_name] = TopologyGroup(
            name=group_name,
            main=main_instance,
            workers=workers,
        )
    return groups


def load_topology_group(group_name: str, *, base_dir: str | None = None) -> TopologyGroup:
    groups = load_topology_groups(base_dir=base_dir)
    normalized = normalize_environment(group_name)
    if normalized not in groups:
        raise KeyError(f"topology group not configured: {normalized}")
    return groups[normalized]


def iter_group_instances(group_name: str, *, base_dir: str | None = None) -> Iterable[str]:
    group = load_topology_group(group_name, base_dir=base_dir)
    return group.instances


def find_topology_group_for_instance(
    instance_name: str | None = None,
    *,
    base_dir: str | None = None,
) -> TopologyGroup | None:
    normalized = normalize_instance_name(get_current_instance_name(instance_name))
    if normalized is None:
        return None

    matched_group: TopologyGroup | None = None
    for group in load_topology_groups(base_dir=base_dir).values():
        if normalized not in group.instances:
            continue
        if matched_group is not None and matched_group.name != group.name:
            raise ValueError(
                f"instance {normalized} configured in multiple topology groups: "
                f"{matched_group.name}, {group.name}"
            )
        matched_group = group
    return matched_group


def resolve_topology_assignment(
    instance_name: str | None = None,
    *,
    base_dir: str | None = None,
) -> TopologyAssignment | None:
    normalized = normalize_instance_name(get_current_instance_name(instance_name))
    if normalized is None:
        return None

    group = find_topology_group_for_instance(normalized, base_dir=base_dir)
    if group is None:
        return None

    role = "main" if normalized == group.main else "executor"
    return TopologyAssignment(
        instance_name=normalized,
        environment=group.environment,
        role=role,
        live_topology_mode=group.live_topology_mode,
    )


def resolve_current_environment(
    environment: str | None = None,
    *,
    instance_name: str | None = None,
    base_dir: str | None = None,
) -> str | None:
    normalized = normalize_environment(environment)
    if normalized is not None:
        return normalized

    normalized_instance = normalize_instance_name(get_current_instance_name(instance_name))
    group = find_topology_group_for_instance(normalized_instance, base_dir=base_dir)
    if group is None:
        return get_current_environment()
    return group.environment
