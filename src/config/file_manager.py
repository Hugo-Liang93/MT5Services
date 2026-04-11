"""
File-backed configuration manager for INI/JSON sources.

This module provides lightweight file watching and direct file access for the
small set of runtime paths that need explicit hot-reload notifications.
"""

from __future__ import annotations

import configparser
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ConfigChangeEvent:
    config_file: str
    section: str
    key: str
    old_value: Any
    new_value: Any
    timestamp: float


class ConfigWatcher:
    """Simple polling watcher for config files."""

    def __init__(self, config_dir: str, check_interval: int = 5):
        self.config_dir = Path(config_dir)
        self.check_interval = check_interval
        self.file_mtimes: Dict[str, float] = {}
        self.callbacks: List[Callable[[ConfigChangeEvent], None]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._init_file_mtimes()

    def iter_config_files(self):
        yield from self.config_dir.glob("*.ini")
        indicators_json = self.config_dir / "indicators.json"
        if indicators_json.exists():
            yield indicators_json

    def _init_file_mtimes(self):
        for config_file in self.iter_config_files():
            try:
                self.file_mtimes[str(config_file)] = config_file.stat().st_mtime
            except OSError:
                continue

    def register_callback(self, callback: Callable[[ConfigChangeEvent], None]):
        self.callbacks.append(callback)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="config-watcher",
            daemon=True,
        )
        self._thread.start()
        logger.info("Config watcher started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Config watcher stopped")

    def _watch_loop(self):
        logger.info("Config watch loop started")
        while not self._stop.is_set():
            try:
                self._check_for_changes()
            except Exception as exc:  # pragma: no cover - defensive log path
                logger.error("Error in config watch loop: %s", exc)
            self._stop.wait(self.check_interval)

    def _check_for_changes(self):
        for config_file in self.iter_config_files():
            file_path = str(config_file)
            try:
                current_mtime = config_file.stat().st_mtime
            except OSError as exc:
                logger.error("Failed to stat config file %s: %s", config_file, exc)
                continue

            last_mtime = self.file_mtimes.get(file_path)
            if last_mtime is None:
                self.file_mtimes[file_path] = current_mtime
                logger.info("New config file detected: %s", config_file.name)
                continue

            if current_mtime <= last_mtime:
                continue

            self.file_mtimes[file_path] = current_mtime
            logger.info("Config file modified: %s", config_file.name)
            event = ConfigChangeEvent(
                config_file=file_path,
                section="*",
                key="*",
                old_value=None,
                new_value=None,
                timestamp=time.time(),
            )
            for callback in self.callbacks:
                try:
                    callback(event)
                except Exception as exc:  # pragma: no cover - defensive log path
                    logger.error("Error in config change callback: %s", exc)


class FileConfigManager:
    """File-backed config accessor with schema-aware conversion."""

    CONFIG_SCHEMA = {
        "app.ini": {
            "trading.symbols": {
                "type": "list",
                "default": ["XAUUSD"],
                "description": "Trading symbol list",
            },
            "trading.timeframes": {
                "type": "list",
                "default": ["M1", "H1"],
                "description": "Trading timeframe list",
            },
            "trading.default_symbol": {
                "type": "str",
                "default": "XAUUSD",
                "description": "Default trading symbol",
            },
            "intervals.poll_interval": {
                "type": "float",
                "default": 0.5,
                "min": 0.1,
                "max": 10.0,
                "description": "Main polling loop interval in seconds",
            },
            "intervals.ohlc_interval": {
                "type": "float",
                "default": 30.0,
                "min": 1.0,
                "max": 300.0,
                "description": "OHLC collection interval in seconds",
            },
            "system.log_level": {
                "type": "str",
                "default": "INFO",
                "allowed": ["DEBUG", "INFO", "WARNING", "ERROR"],
                "description": "Application log level",
            },
        },
        "indicators.json": {
            "pipeline.poll_interval": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 60,
                "description": "Indicator pipeline poll interval in seconds",
            },
            "root.reload_interval": {
                "type": "int",
                "default": 60,
                "min": 10,
                "max": 3600,
                "description": "Indicator config reload interval in seconds",
            },
        },
    }

    def __init__(self, config_dir: str = "config"):
        self.config_dir = Path(config_dir)
        self.configs: Dict[str, Any] = {}
        self.watcher = ConfigWatcher(config_dir)
        self._lock = threading.RLock()
        self._change_callbacks: List[Callable[[str], None]] = []
        self._load_all_configs()
        self.watcher.register_callback(self._on_config_change)
        self.watcher.start()
        logger.info("FileConfigManager initialized with config dir: %s", config_dir)

    def register_change_callback(self, callback: Callable[[str], None]) -> None:
        if callback not in self._change_callbacks:
            self._change_callbacks.append(callback)

    def unregister_change_callback(self, callback: Callable[[str], None]) -> None:
        if callback in self._change_callbacks:
            self._change_callbacks.remove(callback)

    def _load_all_configs(self):
        for config_file in self.watcher.iter_config_files():
            self._load_config(config_file.name)

    def _load_config(self, filename: str):
        config_path = self.config_dir / filename
        if not config_path.exists():
            logger.warning("Config file not found: %s", filename)
            return

        try:
            if config_path.suffix.lower() == ".json":
                with open(config_path, "r", encoding="utf-8") as fh:
                    config = json.load(fh)
            else:
                config = configparser.ConfigParser()
                config.read(config_path, encoding="utf-8")
        except Exception as exc:
            logger.error("Failed to load config %s: %s", filename, exc)
            return

        with self._lock:
            self.configs[filename] = config
        logger.info("Loaded config: %s", filename)

    def reload(self, filename: str, *, notify: bool = True) -> bool:
        config_path = self.config_dir / filename
        if not config_path.exists():
            logger.warning("Config file not found: %s", filename)
            return False
        self._load_config(filename)
        if notify:
            self._notify_config_change(filename)
        return True

    def get(self, filename: str, section: str, key: str, default: Any = None) -> Any:
        schema_key = f"{section}.{key}" if section else key
        schema = self.CONFIG_SCHEMA.get(filename, {}).get(schema_key)

        with self._lock:
            config = self.configs.get(filename)
            if filename.endswith(".json"):
                value = self._get_json_value(config, section, key)
                source = "file" if value is not None else None
            elif config and config.has_option(section, key):
                value = config.get(section, key)
                source = "file"
            else:
                value = None
                source = None

        if value is None:
            if schema and "default" in schema:
                value = schema["default"]
                source = "default"
            else:
                value = default
                source = "param"

        if schema:
            value = self._convert_type(value, schema.get("type", "str"))
            value = self._validate_value(value, schema)

        logger.debug("Config get: %s[%s].%s = %r (source: %s)", filename, section, key, value, source)
        return value

    def _convert_type(self, value: Any, type_name: str) -> Any:
        if value is None:
            return None
        try:
            if type_name == "int":
                return int(value)
            if type_name == "float":
                return float(value)
            if type_name == "bool":
                return str(value).lower() in ["true", "yes", "1", "on"]
            if type_name == "list":
                if isinstance(value, list):
                    return value
                if isinstance(value, str):
                    return [item.strip() for item in value.split(",") if item.strip()]
                return [str(value)]
            return str(value)
        except (ValueError, TypeError) as exc:
            logger.error("Failed to convert value %r to type %s: %s", value, type_name, exc)
            return value

    def _validate_value(self, value: Any, schema: Dict[str, Any]) -> Any:
        if "allowed" in schema and value not in schema["allowed"]:
            logger.warning("Value %r not in allowed values: %s", value, schema["allowed"])
            return schema.get("default", value)
        if isinstance(value, (int, float)):
            if "min" in schema and value < schema["min"]:
                logger.warning("Value %r below minimum %s", value, schema["min"])
                return schema.get("min", value)
            if "max" in schema and value > schema["max"]:
                logger.warning("Value %r above maximum %s", value, schema["max"])
                return schema.get("max", value)
        return value

    def get_all(self, filename: str, section: str) -> Dict[str, Any]:
        with self._lock:
            config = self.configs.get(filename)
            if not config:
                return {}

            if filename.endswith(".json"):
                current = self._get_json_node(config, section)
                if not isinstance(current, dict):
                    return {}
                return {key: self.get(filename, section, key) for key in current.keys()}

            if not config.has_section(section):
                return {}

            return {key: self.get(filename, section, key) for key in config.options(section)}

    def set(self, filename: str, section: str, key: str, value: Any):
        """Mutate the in-memory config snapshot only."""
        with self._lock:
            if filename.endswith(".json"):
                logger.warning(
                    "FileConfigManager does not mutate JSON config in memory; ignoring set(%s, %s, %s)",
                    filename,
                    section,
                    key,
                )
                return

            if filename not in self.configs:
                self.configs[filename] = configparser.ConfigParser()

            config = self.configs[filename]
            if not config.has_section(section):
                config.add_section(section)
            config.set(section, key, str(value))

        logger.info("Config set: %s[%s].%s = %r", filename, section, key, value)

    def _on_config_change(self, event: ConfigChangeEvent):
        logger.info("Config file changed: %s", event.config_file)
        self._load_config(Path(event.config_file).name)
        self._notify_config_change(Path(event.config_file).name)

    def _notify_config_change(self, filename: str) -> None:
        logger.info("Notifying components about config change: %s", filename)
        for cb in list(self._change_callbacks):
            try:
                cb(filename)
            except Exception:
                logger.exception("Config change callback failed for %s: %s", filename, cb)

    def _get_json_node(self, config: Any, section: str) -> Any:
        if not isinstance(config, dict):
            return None
        if not section or section == "root":
            return config

        current = config
        for part in section.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
            if current is None:
                return None
        return current

    def _get_json_value(self, config: Any, section: str, key: str) -> Any:
        node = self._get_json_node(config, section)
        if isinstance(node, dict):
            return node.get(key)
        return None

    def validate_all(self) -> Dict[str, List[str]]:
        errors: Dict[str, List[str]] = {}
        for filename, schema in self.CONFIG_SCHEMA.items():
            file_errors: List[str] = []
            for schema_key, rule in schema.items():
                section, key = schema_key.split(".", 1)
                try:
                    value = self.get(filename, section, key)
                    expected_type = rule.get("type", "str")
                    if not self._check_type(value, expected_type):
                        file_errors.append(
                            f"{schema_key}: expected {expected_type}, got {type(value).__name__}"
                        )
                    if isinstance(value, (int, float)):
                        if "min" in rule and value < rule["min"]:
                            file_errors.append(
                                f"{schema_key}: value {value} below minimum {rule['min']}"
                            )
                        if "max" in rule and value > rule["max"]:
                            file_errors.append(
                                f"{schema_key}: value {value} above maximum {rule['max']}"
                            )
                    if "allowed" in rule and value not in rule["allowed"]:
                        file_errors.append(
                            f"{schema_key}: value {value} not in allowed values {rule['allowed']}"
                        )
                except Exception as exc:
                    file_errors.append(f"{schema_key}: {exc}")
            if file_errors:
                errors[filename] = file_errors
        return errors

    def _check_type(self, value: Any, expected_type: str) -> bool:
        type_map = {
            "int": (int,),
            "float": (float, int),
            "bool": (bool,),
            "str": (str,),
            "list": (list,),
        }
        return isinstance(value, type_map.get(expected_type, (object,)))

    def get_stats(self) -> Dict[str, Any]:
        return {
            "config_files": list(self.configs.keys()),
            "watcher_running": self.watcher.is_running(),
            "validation_errors": self.validate_all(),
        }

    def stop(self):
        self.watcher.stop()
        logger.info("FileConfigManager stopped")


_config_manager_instance: Optional[FileConfigManager] = None


def get_file_config_manager(config_dir: str = "config") -> FileConfigManager:
    global _config_manager_instance
    if _config_manager_instance is None:
        _config_manager_instance = FileConfigManager(config_dir)
    return _config_manager_instance


def close_file_config_manager() -> None:
    global _config_manager_instance
    manager = _config_manager_instance
    _config_manager_instance = None
    if manager is not None:
        manager.stop()
