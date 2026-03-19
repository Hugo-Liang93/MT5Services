"""Orchestration facade for signal runtime components.

This package is a migration entrypoint for moving runtime/voting/state-machine
modules out of the root `src/signals/` namespace without breaking imports.
"""

from src.signals.runtime import SignalRuntime, SignalTarget

__all__ = ["SignalRuntime", "SignalTarget"]
