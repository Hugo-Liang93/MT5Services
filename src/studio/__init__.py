"""Studio — real-time observability layer for the Anteater 3D frontend.

This package aggregates runtime status from all backend modules and exposes
it as a unified protocol (REST + SSE) for frontend consumption.

Architecture:
    - Zero imports from business modules (ingestion/signals/trading/…)
    - Agent status providers are registered as ``Callable[[], dict]`` via
      :meth:`StudioService.register_agent` — wiring happens in ``builder.py``
    - Pure mapper functions in ``mappers.py`` convert raw status dicts to
      StudioAgent dicts — they accept only primitives, never module instances
"""
