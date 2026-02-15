"""Shared Sentry initialization and capture helpers."""

from __future__ import annotations

import logging
import os
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from app.core.config import settings

logger = logging.getLogger(__name__)

_initialized = False

_SENSITIVE_KEYWORDS = (
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
)


def _is_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    return any(keyword in key_lower for keyword in _SENSITIVE_KEYWORDS)


def _sanitize_in_place(value: Any, parent_key: str | None = None) -> Any:
    if parent_key and _is_sensitive_key(parent_key):
        return "[Filtered]"

    if isinstance(value, dict):
        for key, nested_value in list(value.items()):
            if _is_sensitive_key(str(key)):
                value[key] = "[Filtered]"
                continue
            value[key] = _sanitize_in_place(nested_value, str(key))
        return value

    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _sanitize_in_place(item, parent_key)
        return value

    if isinstance(value, tuple):
        return tuple(_sanitize_in_place(item, parent_key) for item in value)

    return value


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    del hint
    return _sanitize_in_place(event)


def _before_breadcrumb(
    crumb: dict[str, Any], hint: dict[str, Any]
) -> dict[str, Any] | None:
    del hint
    return _sanitize_in_place(crumb)


def init_sentry(
    service_name: str,
    enable_fastapi: bool = False,
    enable_celery: bool = False,
    enable_sqlalchemy: bool = False,
    enable_httpx: bool = False,
) -> bool:
    """Initialize Sentry once per process."""
    global _initialized

    if _initialized:
        return True

    dsn = (settings.SENTRY_DSN or "").strip()
    if not dsn:
        logger.info("Sentry disabled: SENTRY_DSN is empty")
        return False

    environment = settings.SENTRY_ENVIRONMENT or settings.ENVIRONMENT
    release = settings.SENTRY_RELEASE or os.getenv("GITHUB_SHA")
    log_event_level = logging.ERROR if settings.SENTRY_ENABLE_LOG_EVENTS else None

    integrations: list[Any] = [
        LoggingIntegration(level=logging.INFO, event_level=log_event_level)
    ]
    if enable_fastapi:
        integrations.append(FastApiIntegration())
    if enable_celery:
        integrations.append(CeleryIntegration())
    if enable_sqlalchemy:
        integrations.append(SqlalchemyIntegration())
    if enable_httpx:
        integrations.append(HttpxIntegration())

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            profiles_sample_rate=settings.SENTRY_PROFILES_SAMPLE_RATE,
            send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
            integrations=integrations,
            before_send=_before_send,
            before_breadcrumb=_before_breadcrumb,
            enable_logs=True,
        )
        sentry_sdk.set_tag("service", service_name)
        sentry_sdk.set_tag("runtime", "python")
        sentry_sdk.set_tag("app", "auto-trader")
        _initialized = True
        logger.info(
            "Sentry initialized: service=%s environment=%s", service_name, environment
        )
        return True
    except Exception:
        logger.exception("Failed to initialize Sentry for service=%s", service_name)
        return False


def capture_exception(exc: BaseException, **context: Any) -> None:
    """Capture an exception with additional context if Sentry is initialized."""
    if not _initialized:
        return

    try:
        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                scope.set_extra(str(key), _sanitize_in_place(value, str(key)))
            sentry_sdk.capture_exception(exc)
    except Exception:
        logger.exception("Failed to capture exception in Sentry")
