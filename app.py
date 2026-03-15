from __future__ import annotations

import logging
import os

from src.config import get_api_config

logger = logging.getLogger("mt5services.launcher")

APP_TARGET = "src.api:app"
DEFAULT_PORT = 8808


def _resolve_host() -> str:
    env_host = os.getenv("MT5_API_HOST")
    if env_host:
        return env_host.strip()
    return get_api_config().host.strip()


def _resolve_port() -> int:
    env_port = os.getenv("MT5_API_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            logger.warning("Invalid MT5_API_PORT '%s', fallback to config/default port", env_port)

    try:
        return int(get_api_config().port)
    except (TypeError, ValueError):
        logger.warning("Invalid configured API port, fallback to default port")
        return DEFAULT_PORT


def resolve_runtime_target() -> tuple[str, str, int]:
    target = APP_TARGET
    host = _resolve_host()
    port = _resolve_port()
    return target, host, port


def launch() -> None:
    import uvicorn

    api_config = get_api_config()
    logging.basicConfig(
        level=logging.INFO,
        format=api_config.log_format,
    )
    app_target, host, port = resolve_runtime_target()
    logger.info("Starting API target=%s host=%s port=%s", app_target, host, port)
    uvicorn.run(app_target, host=host, port=port, reload=False, access_log=api_config.access_log_enabled)


if __name__ == "__main__":
    launch()
