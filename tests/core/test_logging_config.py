from __future__ import annotations

import logging

import pytest

from app.core.logging_config import configure_dependency_log_levels


@pytest.mark.unit
def test_httpx_request_info_is_suppressed_but_warning_is_retained() -> None:
    httpx_logger = logging.getLogger("httpx")
    original_level = httpx_logger.level

    try:
        httpx_logger.setLevel(logging.NOTSET)
        configure_dependency_log_levels()

        assert httpx_logger.level == logging.WARNING
        assert not httpx_logger.isEnabledFor(logging.INFO)
        assert httpx_logger.isEnabledFor(logging.WARNING)
    finally:
        httpx_logger.setLevel(original_level)
