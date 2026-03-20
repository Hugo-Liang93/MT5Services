from __future__ import annotations

from src.config import DBSettings
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter


def create_timescale_writer(db_settings: DBSettings) -> TimescaleWriter:
    return TimescaleWriter(db_settings)


def create_storage_writer(db_settings: DBSettings, storage_settings) -> StorageWriter:
    return StorageWriter(
        create_timescale_writer(db_settings),
        storage_settings=storage_settings,
    )
