from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["TimescaleWriter", "StorageWriter", "DataValidator"]


def __getattr__(name: str) -> Any:
    if name == "TimescaleWriter":
        return import_module("src.persistence.db").TimescaleWriter
    if name == "StorageWriter":
        return import_module("src.persistence.storage_writer").StorageWriter
    if name == "DataValidator":
        return import_module("src.persistence.validator").DataValidator
    raise AttributeError(f"module 'src.persistence' has no attribute {name!r}")
