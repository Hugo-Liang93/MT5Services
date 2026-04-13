from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import get_api_config, get_system_config, reload_configs
from src.config.instance_context import get_current_instance_name, resolve_instance_scoped_dir
from src.entrypoint.logging_context import (
    inject_logging_context,
    install_log_record_context,
)
from src.ops.mt5_session_gate import ensure_mt5_session_gate_or_raise

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


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_log_dir(log_dir_value: str) -> Path:
    log_dir = Path(log_dir_value).expanduser()
    if not log_dir.is_absolute():
        log_dir = _project_root() / log_dir
    return resolve_instance_scoped_dir(log_dir)


def resolve_runtime_target() -> tuple[str, str, int]:
    target = APP_TARGET
    host = _resolve_host()
    port = _resolve_port()
    return target, host, port


def _setup_file_logging(
    system_config,
    formatter: logging.Formatter,
    root_logger: logging.Logger,
) -> None:
    """配置 RotatingFileHandler 日志文件持久化。"""
    if not system_config.log_file_enabled:
        return

    log_dir = _resolve_log_dir(system_config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = system_config.log_file_max_mb * 1024 * 1024
    backup_count = system_config.log_file_backup_count

    # 主日志文件（所有级别）
    main_handler = RotatingFileHandler(
        str(log_dir / "mt5services.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    main_handler.setFormatter(formatter)
    root_logger.addHandler(main_handler)

    # WARNING+ 独立文件（快速排障）
    error_file = system_config.log_error_file
    if error_file:
        error_handler = RotatingFileHandler(
            str(log_dir / error_file),
            maxBytes=max_bytes,
            backupCount=max(3, backup_count // 3),
            encoding="utf-8",
        )
        error_handler.setLevel(logging.WARNING)
        error_handler.setFormatter(formatter)
        root_logger.addHandler(error_handler)

    logger.info(
        "File logging enabled: dir=%s, max=%dMB×%d, error_file=%s",
        log_dir,
        system_config.log_file_max_mb,
        backup_count,
        error_file or "disabled",
    )


def launch() -> None:
    import uvicorn
    from src.utils.timezone import LocalTimeFormatter, configure as configure_tz

    reload_configs()
    api_config = get_api_config()
    system_config = get_system_config()
    instance_name = get_current_instance_name()
    install_log_record_context(instance_name=instance_name)

    # 统一时区：所有日志时间戳使用配置的显示时区
    configure_tz(system_config.timezone)
    formatter = LocalTimeFormatter(inject_logging_context(api_config.log_format))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, str(system_config.log_level).upper(), logging.INFO),
        handlers=[console_handler],
    )

    # 日志文件持久化
    _setup_file_logging(system_config, formatter, logging.getLogger())

    instance_name = get_current_instance_name()
    ensure_mt5_session_gate_or_raise(instance_name=instance_name)
    logger.info(
        "MT5 session gate passed for instance=%s",
        instance_name or "default",
    )

    app_target, host, port = resolve_runtime_target()
    logger.info(
        "Starting API target=%s host=%s port=%s instance=%s",
        app_target,
        host,
        port,
        instance_name or "default",
    )
    uvicorn.run(app_target, host=host, port=port, reload=False, access_log=api_config.access_log_enabled)


if __name__ == "__main__":
    launch()
