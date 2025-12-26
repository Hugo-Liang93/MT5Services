import configparser
import os
from typing import Optional, Tuple


def resolve_config_path(name_or_path: str, base_dir: Optional[str] = None) -> Optional[str]:
    """
    Resolve a config file path.
    - absolute path: return if exists
    - relative/name: resolve to <project_root>/config/<name>
    """
    if not name_or_path:
        return None
    if os.path.isabs(name_or_path):
        return name_or_path if os.path.exists(name_or_path) else None
    root = base_dir or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    path = os.path.abspath(os.path.join(root, "config", name_or_path))
    return path if os.path.exists(path) else None


def load_ini_config(name_or_path: str, base_dir: Optional[str] = None) -> Tuple[Optional[str], Optional[configparser.ConfigParser]]:
    """
    Load an ini config; returns (resolved_path, ConfigParser or None).
    """
    path = resolve_config_path(name_or_path, base_dir)
    if not path:
        return None, None
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return path, parser
