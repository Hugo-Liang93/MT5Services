from __future__ import annotations

import logging
import os
from configparser import ConfigParser
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mt5services.launcher")

PROJECT_ROOT = Path(__file__).resolve().parent
APP_CONFIG_PATH = PROJECT_ROOT / "config" / "app.ini"

MODE_TO_APP = {
    "default": "src.api:app",
    "simple": "src.api.app_simple:app",
    "enhanced": "src.api.app_enhanced:app",
}

MODE_TO_DEFAULT_PORT = {
    "default": 8808,
    "simple": 8810,
    "enhanced": 8810,
}


def _load_config() -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    if APP_CONFIG_PATH.exists():
        parser.read(APP_CONFIG_PATH, encoding="utf-8")
    return parser


def _resolve_mode(parser: ConfigParser, mode_override: Optional[str] = None) -> str:
    if mode_override:
        mode = mode_override.strip().lower()
    else:
        env_mode = os.getenv("MT5_API_MODE")
        mode = env_mode.strip().lower() if env_mode else parser.get(
            "system",
            "api_mode",
            fallback=parser.get("system", "run_mode", fallback="default"),
        ).strip().lower()

    if mode not in MODE_TO_APP:
        logger.warning("Unknown api mode '%s', fallback to 'default'", mode)
        return "default"
    return mode


def _resolve_host(parser: ConfigParser) -> str:
    env_host = os.getenv("MT5_API_HOST")
    if env_host:
        return env_host.strip()
    return parser.get("system", "api_host", fallback="0.0.0.0").strip()


def _resolve_port(parser: ConfigParser, mode: str) -> int:
    env_port = os.getenv("MT5_API_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            logger.warning("Invalid MT5_API_PORT '%s', fallback to config/default", env_port)

    shared_port = parser.get("system", "api_port", fallback="").strip()
    if shared_port:
        try:
            return int(shared_port)
        except ValueError:
            logger.warning("Invalid system.api_port '%s', fallback to mode port", shared_port)

    mode_port_key = f"api_port_{mode}"
    mode_port = parser.get("system", mode_port_key, fallback="").strip()
    if mode_port:
        try:
            return int(mode_port)
        except ValueError:
            logger.warning("Invalid system.%s '%s', fallback to default", mode_port_key, mode_port)

    return MODE_TO_DEFAULT_PORT[mode]


def resolve_runtime_target(mode_override: Optional[str] = None) -> tuple[str, str, int]:
    parser = _load_config()
    mode = _resolve_mode(parser, mode_override=mode_override)
    target = MODE_TO_APP[mode]
    host = _resolve_host(parser)
    port = _resolve_port(parser, mode)
    return target, host, port


def launch(mode_override: Optional[str] = None) -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    app_target, host, port = resolve_runtime_target(mode_override=mode_override)
    logger.info("Starting API target=%s host=%s port=%s", app_target, host, port)
    uvicorn.run(app_target, host=host, port=port, reload=False)


def main(mode_override: Optional[str] = None) -> None:
    launch(mode_override=mode_override)


if __name__ == "__main__":
    main()
