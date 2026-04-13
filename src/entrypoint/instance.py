from __future__ import annotations

import argparse
from pathlib import Path

from src.config import reload_configs
from src.config.instance_context import (
    get_current_instance_name,
    resolve_instance_config_dir,
    set_current_environment,
    set_current_instance_name,
)
from src.config.topology import resolve_topology_assignment
from src.entrypoint.web import launch


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch a named MT5Services instance")
    parser.add_argument("--instance", required=False, help="实例名，对应 config/instances/<instance>")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    instance_name = set_current_instance_name(args.instance or get_current_instance_name())
    if not instance_name:
        raise SystemExit("missing instance name; use --instance <name> or set MT5_INSTANCE")
    instance_dir = resolve_instance_config_dir(instance_name=instance_name)
    if instance_dir is None:
        raise SystemExit(
            f"instance config directory not found: {Path('config') / 'instances' / instance_name}"
        )
    assignment = resolve_topology_assignment(instance_name)
    if assignment is None:
        raise SystemExit(f"instance {instance_name} is not configured in topology.ini")
    set_current_environment(assignment.environment)
    reload_configs()
    launch()


if __name__ == "__main__":
    main()
