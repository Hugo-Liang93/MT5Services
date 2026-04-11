"""IndicatorManager registry facade.

Composes runtime registration helpers and config mutation helpers into a
single module-level API used by manager method binding.
"""

from __future__ import annotations

from .registry_mutation import add_indicator, remove_indicator, update_indicator
from .registry_runtime import (
    _init_components,
    _load_incremental_class,
    _load_indicator_func,
    _register_indicators,
    _reinitialize,
)

__all__ = [
    "_init_components",
    "_load_indicator_func",
    "_load_incremental_class",
    "_register_indicators",
    "_reinitialize",
    "add_indicator",
    "update_indicator",
    "remove_indicator",
]
