from __future__ import annotations

import logging

from src.entrypoint.logging_context import (
    inject_logging_context,
    install_log_record_context,
)


def test_inject_logging_context_prefixes_instance_environment_and_role() -> None:
    fmt = inject_logging_context("%(message)s")

    assert fmt.startswith("[%(environment)s|%(instance)s|%(role)s] ")
    assert fmt.endswith("%(message)s")


def test_inject_logging_context_does_not_duplicate_existing_placeholders() -> None:
    original = "[%(environment)s|%(instance)s|%(role)s] %(message)s"

    assert inject_logging_context(original) == original


def test_install_log_record_context_sets_log_record_fields() -> None:
    previous_factory = logging.getLogRecordFactory()
    try:
        install_log_record_context(
            instance_name="live-main",
            environment="live",
            role="main",
        )
        record = logging.getLogRecordFactory()(
            "test.logger",
            logging.INFO,
            __file__,
            10,
            "hello",
            (),
            None,
        )
    finally:
        logging.setLogRecordFactory(previous_factory)

    assert record.instance == "live-main"
    assert record.environment == "live"
    assert record.role == "main"
