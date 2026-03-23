from __future__ import annotations

import logging

from src.config import get_api_config, get_system_config

logger = logging.getLogger("mt5services.launcher")

APP_TARGET = "src.api:app"
DEFAULT_PORT = 8808


def _resolve_host() -> str:
    return get_api_config().host.strip()


def _resolve_port() -> int:
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
    from src.utils.timezone import LocalTimeFormatter, configure as configure_tz

    api_config = get_api_config()
    system_config = get_system_config()

    # 统一时区：所有日志时间戳使用配置的显示时区
    configure_tz(system_config.timezone)
    handler = logging.StreamHandler()
    handler.setFormatter(LocalTimeFormatter(api_config.log_format))
    logging.basicConfig(
        level=getattr(logging, str(system_config.log_level).upper(), logging.INFO),
        handlers=[handler],
    )

    app_target, host, port = resolve_runtime_target()
    logger.info("Starting API target=%s host=%s port=%s", app_target, host, port)
    uvicorn.run(app_target, host=host, port=port, reload=False, access_log=api_config.access_log_enabled)


if __name__ == "__main__":
    launch()
