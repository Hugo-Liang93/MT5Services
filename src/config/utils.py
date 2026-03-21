import configparser
import os
from typing import Any, Dict, Optional, Tuple


def resolve_config_path(name_or_path: str, base_dir: Optional[str] = None) -> Optional[str]:
    """Resolve config path relative to <project_root>/config for non-absolute input."""
    if not name_or_path:
        return None
    if os.path.isabs(name_or_path):
        return name_or_path if os.path.exists(name_or_path) else None
    root = base_dir or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    path = os.path.abspath(os.path.join(root, "config", name_or_path))
    return path if os.path.exists(path) else None


def load_ini_config(
    name_or_path: str, base_dir: Optional[str] = None
) -> Tuple[Optional[str], Optional[configparser.ConfigParser]]:
    """Load ini file as raw values (no interpolation)."""
    path = resolve_config_path(name_or_path, base_dir)
    if not path:
        return None, None
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return path, parser


def load_config_with_base(
    config_name: str, base_config: str = "app.ini", base_dir: Optional[str] = None
) -> Tuple[Optional[str], Optional[configparser.ConfigParser]]:
    """Load target config with layered overrides.

    Merge order (later overrides earlier):
    1) base config, e.g. app.ini
    2) base local override, e.g. app.local.ini
    3) target config, e.g. market.ini
    4) target local override, e.g. market.local.ini
    """
    base_path, base_parser = load_ini_config(base_config, base_dir=base_dir)
    if not base_parser:
        return load_ini_config(config_name, base_dir=base_dir)

    base_local_name = (
        base_config[:-4] + ".local.ini" if base_config.endswith(".ini") else base_config + ".local"
    )
    _, base_local_parser = load_ini_config(base_local_name, base_dir=base_dir)

    target_path, target_parser = load_ini_config(config_name, base_dir=base_dir)
    target_local_name = (
        config_name[:-4] + ".local.ini" if config_name.endswith(".ini") else config_name + ".local"
    )
    _, target_local_parser = load_ini_config(target_local_name, base_dir=base_dir)

    if not target_parser and not target_local_parser and not base_local_parser:
        return base_path, base_parser

    merged_parser = configparser.ConfigParser(interpolation=None)

    for section in base_parser.sections():
        if not merged_parser.has_section(section):
            merged_parser.add_section(section)
        for key, value in base_parser.items(section):
            merged_parser.set(section, key, value)

    if base_local_parser:
        for section in base_local_parser.sections():
            if not merged_parser.has_section(section):
                merged_parser.add_section(section)
            for key, value in base_local_parser.items(section):
                merged_parser.set(section, key, value)

    if target_parser:
        for section in target_parser.sections():
            if not merged_parser.has_section(section):
                merged_parser.add_section(section)
            for key, value in target_parser.items(section):
                merged_parser.set(section, key, value)

    if target_local_parser:
        for section in target_local_parser.sections():
            if not merged_parser.has_section(section):
                merged_parser.add_section(section)
            for key, value in target_local_parser.items(section):
                merged_parser.set(section, key, value)

    return target_path, merged_parser


def get_merged_option_source(
    config_name: str,
    section: str,
    key: str,
    base_config: str = "app.ini",
    base_dir: Optional[str] = None,
) -> Optional[str]:
    """Resolve which layered INI file contributed the final value for a key."""
    if not section or not key:
        return None

    def _local_name(filename: str) -> str:
        return filename[:-4] + ".local.ini" if filename.endswith(".ini") else filename + ".local"

    _, base_parser = load_ini_config(base_config, base_dir=base_dir)
    base_local_name = _local_name(base_config)
    _, base_local_parser = load_ini_config(base_local_name, base_dir=base_dir)
    _, target_parser = load_ini_config(config_name, base_dir=base_dir)
    target_local_name = _local_name(config_name)
    _, target_local_parser = load_ini_config(target_local_name, base_dir=base_dir)

    candidates = []
    if config_name == base_config:
        candidates.extend(
            [
                (base_local_name, base_local_parser),
                (base_config, base_parser),
            ]
        )
    else:
        candidates.extend(
            [
                (target_local_name, target_local_parser),
                (config_name, target_parser),
                (base_local_name, base_local_parser),
                (base_config, base_parser),
            ]
        )

    for filename, parser in candidates:
        if parser and parser.has_section(section) and parser.has_option(section, key):
            return f"{filename}[{section}].{key}"
    return None


def get_merged_config(config_name: str) -> Dict[str, Any]:
    """Return merged config as nested dict."""
    _, parser = load_config_with_base(config_name)
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
