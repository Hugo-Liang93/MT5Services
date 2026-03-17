from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_utils_module():
    root = Path(__file__).resolve().parents[2]
    utils_path = root / "src" / "config" / "utils.py"
    spec = importlib.util.spec_from_file_location("config_utils_for_test", utils_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, content: str) -> None:
    path.write_text(content.strip() + "\n", encoding="utf-8")


def test_local_ini_overrides_target_and_base(tmp_path: Path):
    utils = _load_utils_module()
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    _write(
        config_dir / "app.ini",
        """
        [system]
        api_host = 0.0.0.0
        api_port = 8808
        """,
    )
    _write(
        config_dir / "market.ini",
        """
        [api]
        host = 127.0.0.1
        port = 8809

        [security]
        api_key = in_repo
        """,
    )
    _write(
        config_dir / "market.local.ini",
        """
        [api]
        port = 8810

        [security]
        api_key = local_secret
        """,
    )

    _, parser = utils.load_config_with_base("market.ini", base_dir=str(tmp_path))
    assert parser is not None

    assert parser.get("api", "host") == "127.0.0.1"
    assert parser.get("api", "port") == "8810"
    assert parser.get("security", "api_key") == "local_secret"


def test_base_local_ini_can_override_app_defaults(tmp_path: Path):
    utils = _load_utils_module()
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    _write(
        config_dir / "app.ini",
        """
        [system]
        api_port = 8808
        """,
    )
    _write(
        config_dir / "app.local.ini",
        """
        [system]
        api_port = 8899
        """,
    )

    _, parser = utils.load_config_with_base("app.ini", base_dir=str(tmp_path))
    assert parser is not None
    assert parser.get("system", "api_port") == "8899"


def test_get_merged_option_source_prefers_local_layers(tmp_path: Path):
    utils = _load_utils_module()
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    _write(
        config_dir / "app.ini",
        """
        [system]
        api_port = 8808
        """,
    )
    _write(
        config_dir / "app.local.ini",
        """
        [system]
        api_port = 8899
        """,
    )
    _write(
        config_dir / "market.ini",
        """
        [api]
        host = 0.0.0.0
        """,
    )
    _write(
        config_dir / "market.local.ini",
        """
        [api]
        host = 127.0.0.1
        """,
    )

    assert (
        utils.get_merged_option_source("app.ini", "system", "api_port", base_dir=str(tmp_path))
        == "app.local.ini[system].api_port"
    )
    assert (
        utils.get_merged_option_source("market.ini", "api", "host", base_dir=str(tmp_path))
        == "market.local.ini[api].host"
    )
