from __future__ import annotations

import logging
import os
from configparser import ConfigParser
from pathlib import Path

logger = logging.getLogger("mt5services.launcher")

PROJECT_ROOT = Path(__file__).resolve().parent
APP_CONFIG_PATH = PROJECT_ROOT / "config" / "app.ini"

APP_TARGET = "src.api:app"
DEFAULT_PORT = 8808


def _load_config() -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    if APP_CONFIG_PATH.exists():
        parser.read(APP_CONFIG_PATH, encoding="utf-8")
    return parser


def _resolve_host(parser: ConfigParser) -> str:
    env_host = os.getenv("MT5_API_HOST")
    if env_host:
        return env_host.strip()
    return parser.get("system", "api_host", fallback="0.0.0.0").strip()


def _resolve_port(parser: ConfigParser) -> int:
    env_port = os.getenv("MT5_API_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            logger.warning("Invalid MT5_API_PORT '%s', fallback to config/default port", env_port)

    shared_port = parser.get("system", "api_port", fallback="").strip()
    if shared_port:
        try:
            return int(shared_port)
        except ValueError:
            logger.warning("Invalid system.api_port '%s', fallback to default port", shared_port)
    return DEFAULT_PORT


def resolve_runtime_target() -> tuple[str, str, int]:
    parser = _load_config()
    target = APP_TARGET
    host = _resolve_host(parser)
    port = _resolve_port(parser)
    return target, host, port


def launch() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    app_target, host, port = resolve_runtime_target()
    logger.info("Starting API target=%s host=%s port=%s", app_target, host, port)
    uvicorn.run(app_target, host=host, port=port, reload=False)


if __name__ == "__main__":
    launch()
