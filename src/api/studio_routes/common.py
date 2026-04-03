from __future__ import annotations

from src.studio.service import StudioService


def get_studio_service() -> StudioService:
    from src.api.deps import get_studio_service as resolve_studio_service

    return resolve_studio_service()
