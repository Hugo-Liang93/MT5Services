"""Application runtime: container, builder, and lifecycle management."""

from src.app_runtime.container import AppContainer
from src.app_runtime.runtime import AppRuntime


def build_app_container(*args, **kwargs):
    from src.app_runtime.builder import build_app_container as _build_app_container

    return _build_app_container(*args, **kwargs)


__all__ = ["AppContainer", "build_app_container", "AppRuntime"]
