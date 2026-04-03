from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol


class RuntimeManagedComponent(Protocol):
    name: str
    supported_modes: frozenset[str]

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def is_running(self) -> bool: ...

    def snapshot(self) -> dict[str, Any]: ...


@dataclass
class FunctionalRuntimeComponent:
    name: str
    supported_modes: frozenset[str]
    start_fn: Callable[[], None]
    stop_fn: Callable[[], None]
    is_running_fn: Callable[[], bool]
    metadata: dict[str, Any] = field(default_factory=dict)

    def start(self) -> None:
        self.start_fn()

    def stop(self) -> None:
        self.stop_fn()

    def is_running(self) -> bool:
        return bool(self.is_running_fn())

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.is_running(),
            "supported_modes": sorted(self.supported_modes),
            **dict(self.metadata),
        }

class RuntimeComponentRegistry:
    def __init__(self, components: Iterable[RuntimeManagedComponent] = ()) -> None:
        self._components: "OrderedDict[str, RuntimeManagedComponent]" = OrderedDict()
        for component in components:
            self.register(component)

    def register(self, component: RuntimeManagedComponent) -> None:
        self._components[component.name] = component

    def get(self, name: str) -> RuntimeManagedComponent | None:
        return self._components.get(name)

    def apply_mode(self, mode: str) -> None:
        target_mode = str(mode).strip().lower()
        for component in self._components.values():
            if target_mode in component.supported_modes:
                component.start()
        for component in reversed(tuple(self._components.values())):
            if target_mode not in component.supported_modes:
                component.stop()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            name: component.is_running()
            for name, component in self._components.items()
        }

    def detailed_snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            name: component.snapshot()
            for name, component in self._components.items()
        }
