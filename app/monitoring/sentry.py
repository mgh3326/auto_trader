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

try:
    from sentry_sdk.integrations.mcp import MCPIntegration
except ImportError:  # pragma: no cover - dependent on sentry-sdk version
    MCPIntegration = None

from app.core.config import settings

logger = logging.getLogger(__name__)

_initialized = False
_enabled_integration_flags: dict[str, bool] = {
    "fastapi": False,
    "celery": False,
    "sqlalchemy": False,
    "httpx": False,
    "mcp": False,
}

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


def _is_healthcheck_access_log(logger_name: str | None, message: str | None) -> bool:
    if logger_name != "uvicorn.access" or not message:
        return False
    return "/healthz" in message and '" 200' in message


def _extract_log_context(
    payload: dict[str, Any], hint: dict[str, Any]
) -> tuple[str | None, str | None]:
    logger_name: str | None = None
    message: str | None = None

    log_record = hint.get("log_record")
    if log_record is not None:
        record_name = getattr(log_record, "name", None)
        if isinstance(record_name, str):
            logger_name = record_name
        get_message = getattr(log_record, "getMessage", None)
        if callable(get_message):
            maybe_message = get_message()
            if isinstance(maybe_message, str):
                message = maybe_message

    payload_logger = payload.get("logger")
    if logger_name is None and isinstance(payload_logger, str):
        logger_name = payload_logger

    payload_logentry = payload.get("logentry")
    if isinstance(payload_logentry, dict):
        formatted = payload_logentry.get("formatted")
        template = payload_logentry.get("message")
        if isinstance(formatted, str):
            message = formatted
        elif message is None and isinstance(template, str):
            message = template

    payload_message = payload.get("message")
    if message is None and isinstance(payload_message, str):
        message = payload_message

    return logger_name, message


def _extract_sentry_log_context(
    sentry_log: dict[str, Any], hint: dict[str, Any]
) -> tuple[str | None, str | None]:
    logger_name: str | None = None
    attributes = sentry_log.get("attributes")
    if isinstance(attributes, dict):
        attr_logger = attributes.get("logger.name")
        if isinstance(attr_logger, str):
            logger_name = attr_logger

    body = sentry_log.get("body")
    message = body if isinstance(body, str) else None

    context_logger, context_message = _extract_log_context({}, hint)
    return logger_name or context_logger, message or context_message


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


def _before_send(
    event: dict[str, Any], hint: dict[str, Any]
) -> dict[str, Any] | None:
    logger_name, message = _extract_log_context(event, hint)
    if _is_healthcheck_access_log(logger_name, message):
        return None
    return _sanitize_in_place(event)


def _before_breadcrumb(
    crumb: dict[str, Any], hint: dict[str, Any]
) -> dict[str, Any] | None:
    del hint
    category = crumb.get("category")
    message = crumb.get("message")
    if isinstance(category, str) and isinstance(message, str):
        if _is_healthcheck_access_log(category, message):
            return None
    return _sanitize_in_place(crumb)


def _before_send_log(
    sentry_log: dict[str, Any], hint: dict[str, Any]
) -> dict[str, Any] | None:
    logger_name, message = _extract_sentry_log_context(sentry_log, hint)
    if _is_healthcheck_access_log(logger_name, message):
        return None
    return _sanitize_in_place(sentry_log)


def init_sentry(
    service_name: str,
    enable_fastapi: bool = False,
    enable_celery: bool = False,
    enable_sqlalchemy: bool = False,
    enable_httpx: bool = False,
    enable_mcp: bool = False,
) -> bool:
    """Initialize Sentry once per process."""
    global _enabled_integration_flags, _initialized

    requested_flags = {
        "fastapi": enable_fastapi,
        "celery": enable_celery,
        "sqlalchemy": enable_sqlalchemy,
        "httpx": enable_httpx,
        "mcp": enable_mcp,
    }
    effective_flags = {
        key: _enabled_integration_flags[key] or requested_flags[key]
        for key in _enabled_integration_flags
    }

    if _initialized and effective_flags == _enabled_integration_flags:
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
    if effective_flags["fastapi"]:
        integrations.append(FastApiIntegration())
    if effective_flags["celery"]:
        integrations.append(CeleryIntegration())
    if effective_flags["sqlalchemy"]:
        integrations.append(SqlalchemyIntegration())
    if effective_flags["httpx"]:
        integrations.append(HttpxIntegration())
    if effective_flags["mcp"]:
        if MCPIntegration is None:
            logger.warning(
                "Sentry MCP integration unavailable in current sentry-sdk version"
            )
            effective_flags["mcp"] = False
        else:
            integrations.append(
                MCPIntegration(
                    include_prompts=settings.SENTRY_MCP_INCLUDE_PROMPTS,
                )
            )

    try:
        if _initialized:
            logger.info(
                "Reinitializing Sentry to add integrations: previous=%s requested=%s",
                sorted(key for key, enabled in _enabled_integration_flags.items() if enabled),
                sorted(key for key, enabled in effective_flags.items() if enabled),
            )
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
            before_send_log=_before_send_log,
            enable_logs=True,
        )
        sentry_sdk.set_tag("service", service_name)
        sentry_sdk.set_tag("runtime", "python")
        sentry_sdk.set_tag("app", "auto-trader")
        _initialized = True
        _enabled_integration_flags = effective_flags
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
