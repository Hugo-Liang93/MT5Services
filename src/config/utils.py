import configparser
import os
from typing import Any, Dict, Optional, Tuple

from src.config.instance_context import (
    get_current_instance_name,
    normalize_instance_name,
    resolve_instance_config_dir,
)


_INSTANCE_SCOPED_CONFIGS = {
    "market.ini",
    "mt5.ini",
    "risk.ini",
    "exit.ini",
}


def _canonical_config_name(name_or_path: str) -> str | None:
    if not name_or_path or os.path.isabs(name_or_path):
        return None
    filename = os.path.basename(name_or_path).strip()
    if not filename:
        return None
    if filename.endswith(".local.ini"):
        return filename[: -len(".local.ini")] + ".ini"
    return filename


def _allows_instance_layers(name_or_path: str) -> bool:
    canonical = _canonical_config_name(name_or_path)
    return canonical in _INSTANCE_SCOPED_CONFIGS


def _project_root(base_dir: Optional[str] = None) -> str:
    return base_dir or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _config_root(base_dir: Optional[str] = None) -> str:
    return os.path.abspath(os.path.join(_project_root(base_dir), "config"))


def _local_name(filename: str) -> str:
    return filename[:-4] + ".local.ini" if filename.endswith(".ini") else filename + ".local"


def _resolve_named_config_path(
    name_or_path: str,
    *,
    base_dir: Optional[str] = None,
    instance_name: Optional[str] = None,
) -> Optional[str]:
    if not name_or_path:
        return None
    if os.path.isabs(name_or_path):
        return name_or_path if os.path.exists(name_or_path) else None
    if _allows_instance_layers(name_or_path):
        instance_dir = resolve_instance_config_dir(
            instance_name=instance_name,
            base_dir=_project_root(base_dir),
        )
        if instance_dir is not None:
            candidate = instance_dir / name_or_path
            if candidate.exists():
                return str(candidate)
    candidate = os.path.abspath(os.path.join(_config_root(base_dir), name_or_path))
    return candidate if os.path.exists(candidate) else None


def _read_ini(path: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def _merge_parsers(*parsers: Optional[configparser.ConfigParser]) -> Optional[configparser.ConfigParser]:
    merged_parser = configparser.ConfigParser(interpolation=None)
    merged_any = False
    for parser in parsers:
        if parser is None:
            continue
        merged_any = True
        for section in parser.sections():
            if not merged_parser.has_section(section):
                merged_parser.add_section(section)
            for key, value in parser.items(section):
                merged_parser.set(section, key, value)
    return merged_parser if merged_any else None


def _load_named_config_layers(
    name_or_path: str,
    *,
    base_dir: Optional[str] = None,
    instance_name: Optional[str] = None,
) -> list[tuple[str, configparser.ConfigParser]]:
    layers: list[tuple[str, configparser.ConfigParser]] = []
    global_path = os.path.abspath(os.path.join(_config_root(base_dir), name_or_path))
    if os.path.exists(global_path):
        layers.append((name_or_path, _read_ini(global_path)))
    normalized_instance = normalize_instance_name(get_current_instance_name(instance_name))
    if normalized_instance is not None and _allows_instance_layers(name_or_path):
        instance_dir = resolve_instance_config_dir(
            instance_name=normalized_instance,
            base_dir=_project_root(base_dir),
        )
        if instance_dir is not None:
            instance_path = instance_dir / name_or_path
            if instance_path.exists():
                label = f"instances/{normalized_instance}/{name_or_path}"
                layers.append((label, _read_ini(str(instance_path))))
    return layers


def resolve_config_path(name_or_path: str, base_dir: Optional[str] = None) -> Optional[str]:
    """Resolve config path relative to <project_root>/config for non-absolute input."""
    if not name_or_path:
        return None
    if os.path.isabs(name_or_path):
        return name_or_path if os.path.exists(name_or_path) else None
    return _resolve_named_config_path(name_or_path, base_dir=base_dir)


def load_ini_config(
    name_or_path: str,
    base_dir: Optional[str] = None,
    *,
    instance_name: Optional[str] = None,
) -> Tuple[Optional[str], Optional[configparser.ConfigParser]]:
    """Load ini file as raw values (no interpolation)."""
    path = _resolve_named_config_path(
        name_or_path,
        base_dir=base_dir,
        instance_name=instance_name,
    )
    if not path:
        return None, None
    return path, _read_ini(path)


def load_config_with_base(
    config_name: str,
    base_config: str = "app.ini",
    base_dir: Optional[str] = None,
    *,
    instance_name: Optional[str] = None,
) -> Tuple[Optional[str], Optional[configparser.ConfigParser]]:
    """Load target config with layered overrides.

    Merge order (later overrides earlier):
    1) base config, e.g. app.ini
    2) base local override, e.g. app.local.ini
    3) instance base override, e.g. config/instances/live-main/app.ini
    4) instance base local override
    5) target config, e.g. market.ini
    6) target local override, e.g. market.local.ini
    7) instance target override
    8) instance target local override

    Note:
    - Only explicitly instance-scoped configs participate in steps 3/4/7/8.
    - Shared configs such as app.ini/db.ini/signal.ini/topology.ini remain
      rooted at config/*.ini and config/*.local.ini only.
    """
    normalized_instance = normalize_instance_name(get_current_instance_name(instance_name))
    base_local_name = _local_name(base_config)
    target_local_name = _local_name(config_name)

    layers: list[tuple[str, configparser.ConfigParser]] = []
    layers.extend(
        _load_named_config_layers(
            base_config,
            base_dir=base_dir,
            instance_name=normalized_instance,
        )
    )
    layers.extend(
        _load_named_config_layers(
            base_local_name,
            base_dir=base_dir,
            instance_name=normalized_instance,
        )
    )
    if config_name != base_config:
        layers.extend(
            _load_named_config_layers(
                config_name,
                base_dir=base_dir,
                instance_name=normalized_instance,
            )
        )
        layers.extend(
            _load_named_config_layers(
                target_local_name,
                base_dir=base_dir,
                instance_name=normalized_instance,
            )
        )

    if not layers:
        return None, None

    parser = _merge_parsers(*(item[1] for item in layers))
    target_path = _resolve_named_config_path(
        config_name,
        base_dir=base_dir,
        instance_name=normalized_instance,
    )
    if target_path is None and config_name == base_config:
        target_path = _resolve_named_config_path(
            base_config,
            base_dir=base_dir,
            instance_name=normalized_instance,
        )
    return target_path, parser


def get_merged_option_source(
    config_name: str,
    section: str,
    key: str,
    base_config: str = "app.ini",
    base_dir: Optional[str] = None,
    *,
    instance_name: Optional[str] = None,
) -> Optional[str]:
    """Resolve which layered INI file contributed the final value for a key."""
    if not section or not key:
        return None

    normalized_instance = normalize_instance_name(get_current_instance_name(instance_name))

    def _reverse_layers(filename: str) -> list[tuple[str, configparser.ConfigParser]]:
        return list(
            reversed(
                _load_named_config_layers(
                    filename,
                    base_dir=base_dir,
                    instance_name=normalized_instance,
                )
            )
        )

    candidates: list[tuple[str, configparser.ConfigParser]] = []
    if config_name == base_config:
        candidates.extend(_reverse_layers(_local_name(base_config)))
        candidates.extend(_reverse_layers(base_config))
    else:
        candidates.extend(_reverse_layers(_local_name(config_name)))
        candidates.extend(_reverse_layers(config_name))
        candidates.extend(_reverse_layers(_local_name(base_config)))
        candidates.extend(_reverse_layers(base_config))

    for filename, parser in candidates:
        if parser and parser.has_section(section) and parser.has_option(section, key):
            return f"{filename}[{section}].{key}"
    return None


def get_merged_config(config_name: str, *, instance_name: Optional[str] = None) -> Dict[str, Any]:
    """Return merged config as nested dict."""
    _, parser = load_config_with_base(config_name, instance_name=instance_name)
    if not parser:
        return {}
    return {section: dict(parser.items(section)) for section in parser.sections()}


class ConfigValidator:
    """Small validator set for centralized config bootstrap."""

    @staticmethod
    def validate_trading_config(config: Dict[str, Any]) -> bool:
        trading = config.get("trading", {})

        symbols_raw = trading.get("symbols", "")
        if isinstance(symbols_raw, list):
            symbols = [str(s).strip() for s in symbols_raw if str(s).strip()]
        else:
            symbols = [s.strip() for s in str(symbols_raw).split(",") if s.strip()]

        default_symbol = trading.get("default_symbol", "")
        if not symbols:
            raise ValueError("No trading symbols configured")
        if default_symbol and default_symbol not in symbols:
            raise ValueError(f"Default symbol '{default_symbol}' not in symbols list: {symbols}")

        timeframes_raw = trading.get("timeframes", "")
        if isinstance(timeframes_raw, list):
            timeframes = [str(t).strip() for t in timeframes_raw if str(t).strip()]
        else:
            timeframes = [t.strip() for t in str(timeframes_raw).split(",") if t.strip()]

        valid_timeframes = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"]
        for tf in timeframes:
            if tf not in valid_timeframes:
                raise ValueError(f"Invalid timeframe: {tf}. Valid: {valid_timeframes}")
        return True

    @staticmethod
    def validate_interval_config(config: Dict[str, Any]) -> bool:
        intervals = config.get("intervals", {})
        poll_interval = float(intervals.get("poll_interval", 0.5))
        if poll_interval < 0.1:
            raise ValueError(f"Poll interval too small: {poll_interval}")

        ohlc_interval = float(intervals.get("ohlc_interval", 30.0))
        if ohlc_interval < 1.0:
            raise ValueError(f"OHLC interval too small: {ohlc_interval}")
        return True
