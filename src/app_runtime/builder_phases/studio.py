"""Frontend observability phase builders."""

from __future__ import annotations

from src.app_runtime.container import AppContainer
from src.studio.runtime import build_studio_service


def build_studio_service_layer(container: AppContainer) -> None:
    container.studio_service = build_studio_service(container)
