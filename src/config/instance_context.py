from __future__ import annotations

import os
from pathlib import Path

INSTANCE_ENV_VAR = "MT5_INSTANCE"
ENVIRONMENT_ENV_VAR = "MT5_ENVIRONMENT"


def normalize_instance_name(instance_name: str | None) -> str | None:
    normalized = str(instance_name or "").strip()
    return normalized or None


def normalize_environment(environment: str | None) -> str | None:
    normalized = str(environment or "").strip().lower()
    if not normalized:
        return None
    if normalized not in {"live", "demo"}:
        raise ValueError(f"unsupported environment: {environment!r}")
    return normalized


def get_current_instance_name(instance_name: str | None = None) -> str | None:
    normalized = normalize_instance_name(instance_name)
    if normalized is not None:
        return normalized
    return normalize_instance_name(os.getenv(INSTANCE_ENV_VAR))


def get_current_environment(environment: str | None = None) -> str | None:
    normalized = normalize_environment(environment)
    if normalized is not None:
        return normalized
    return normalize_environment(os.getenv(ENVIRONMENT_ENV_VAR))


def set_current_instance_name(instance_name: str | None) -> str | None:
    normalized = normalize_instance_name(instance_name)
    if normalized is None:
        os.environ.pop(INSTANCE_ENV_VAR, None)
        return None
    os.environ[INSTANCE_ENV_VAR] = normalized
    return normalized


def set_current_environment(environment: str | None) -> str | None:
    normalized = normalize_environment(environment)
    if normalized is None:
        os.environ.pop(ENVIRONMENT_ENV_VAR, None)
        return None
    os.environ[ENVIRONMENT_ENV_VAR] = normalized
    return normalized


def resolve_instance_config_dir(
    *,
    instance_name: str | None = None,
    base_dir: str | None = None,
) -> Path | None:
    normalized = get_current_instance_name(instance_name)
    if normalized is None:
        return None
    root = Path(base_dir or Path(__file__).resolve().parents[2])
    instance_dir = root / "config" / "instances" / normalized
    return instance_dir if instance_dir.exists() else None


def resolve_instance_scoped_dir(
    base_path: str | Path,
    *,
    instance_name: str | None = None,
) -> Path:
    path = Path(base_path).expanduser()
    normalized = get_current_instance_name(instance_name)
    if normalized is None:
        return path
    return path / normalized
