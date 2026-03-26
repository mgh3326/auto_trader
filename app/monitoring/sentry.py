"""Shared Sentry initialization and capture helpers."""

from __future__ import annotations

import copy
import logging
import subprocess
from pathlib import Path
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.types import Breadcrumb, Event, Hint, Log

try:
    from sentry_sdk.integrations.mcp import MCPIntegration
except ImportError:  # pragma: no cover - dependent on sentry-sdk version
    MCPIntegration = None

try:
    from mcp.server.lowlevel.server import request_ctx as _mcp_request_ctx
except ImportError:  # pragma: no cover - dependent on MCP SDK availability
    _mcp_request_ctx = None

from app.core.config import settings

logger = logging.getLogger(__name__)

_BUILD_VCS_REF_PATH = Path("/app/.build-vcs-ref")

_initialized = False
_enabled_integration_flags: dict[str, bool] = {
    "fastapi": False,
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


_YFINANCE_CRUMB_PATTERNS = ("invalid crumb", "invalid cookie")


def _is_yfinance_crumb_error(logger_name: str | None, message: str | None) -> bool:
    if not logger_name or not isinstance(logger_name, str):
        return False
    if not logger_name.startswith("yfinance"):
        return False
    if not message or not isinstance(message, str):
        return False
    msg_lower = message.lower()
    return any(pattern in msg_lower for pattern in _YFINANCE_CRUMB_PATTERNS)


_YFINANCE_NOISE_PATTERNS = (
    "possibly delisted",
    "No data found",
    "no price data found",
    "symbol may be delisted",
    "Quote not found",
)


def _is_yfinance_noise_log(logger_name: str | None, message: str | None) -> bool:
    if logger_name != "yfinance" or not message:
        return False
    return any(pattern in message for pattern in _YFINANCE_NOISE_PATTERNS)


def _is_yfinance_html_500_noise(logger_name: str | None, message: str | None) -> bool:
    """yfinance 'HTTP Error 500' unknown host error is expected Yahoo outage noise."""
    if logger_name != "yfinance" or not message:
        return False
    return (
        "HTTP Error 500" in message
        and ("Unknown Host" in message or "Will be right back" in message)
        and ("quoteSummary/" in message or "<!DOCTYPE html>" in message)
    )


def _is_fastmcp_tool_validation_error(
    logger_name: str | None, message: str | None
) -> bool:
    if logger_name != "fastmcp.server.server" or not message:
        return False
    return message.startswith("Error validating tool ")


# Rules for expected MCP argument noise (client misuse, not server failures)
_EXPECTED_MCP_ARGUMENT_NOISE_RULES = (
    {
        "tool_name": "get_order_history",
        "message_snippet": "symbol is required when status=",
        "log_prefix": "Error calling tool 'get_order_history'",
    },
    {
        "tool_name": "get_short_interest",
        "message_snippet": "Short selling data is only available for Korean stocks",
        "log_prefix": "Error calling tool 'get_short_interest'",
    },
    {
        "tool_name": "get_company_profile",
        "message_snippet": "Company profile is not available for cryptocurrencies",
        "log_prefix": "Error calling tool 'get_company_profile'",
    },
    {
        "tool_name": "get_valuation",
        "message_snippet": "Valuation metrics are not available for cryptocurrencies",
        "log_prefix": "Error calling tool 'get_valuation'",
    },
)


def _is_expected_mcp_argument_noise(
    logger_name: str | None,
    message: str | None,
    event: Event | None = None,
) -> bool:
    # Check log path first (fastmcp.server.server logger)
    if logger_name == "fastmcp.server.server" and message:
        for rule in _EXPECTED_MCP_ARGUMENT_NOISE_RULES:
            if (
                message.startswith(rule["log_prefix"])
                and rule["message_snippet"] in message
            ):
                return True

    if not event:
        return False

    # Collect all possible tool name indicators from the event
    tool_names: set[str] = set()

    # From tags
    tags = event.get("tags", {})
    if isinstance(tags, dict):
        tag_tool_name = tags.get("mcp.tool.name")
        if isinstance(tag_tool_name, str):
            tool_names.add(tag_tool_name)

    # From contexts
    contexts = event.get("contexts", {})
    if isinstance(contexts, dict):
        mcp_tool_call = contexts.get("mcp_tool_call", {})
        if isinstance(mcp_tool_call, dict):
            ctx_tool_name = mcp_tool_call.get("tool_name")
            if isinstance(ctx_tool_name, str):
                tool_names.add(ctx_tool_name)

    # From exception values
    values = event.get("exception", {}).get("values", [])
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, dict):
                continue
            exc_type = value.get("type")
            exc_value = value.get("value")
            if not (
                exc_type in {"ToolError", "ValueError"} and isinstance(exc_value, str)
            ):
                continue
            # Extract tool name from exception value (e.g., "Error calling tool 'X': ...")
            for rule in _EXPECTED_MCP_ARGUMENT_NOISE_RULES:
                if rule["log_prefix"] in exc_value:
                    tool_names.add(rule["tool_name"])

    # Check if any matched tool has the expected message snippet
    for rule in _EXPECTED_MCP_ARGUMENT_NOISE_RULES:
        if rule["tool_name"] not in tool_names:
            continue
        # Check exception values for message snippet
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, dict):
                    continue
                exc_value = value.get("value")
                if isinstance(exc_value, str) and rule["message_snippet"] in exc_value:
                    return True

    return False


def _extract_log_context(payload: Event, hint: Hint) -> tuple[str | None, str | None]:
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
    sentry_log: Log, hint: Hint
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


def _before_send(event: Event, hint: Hint) -> Event | None:
    logger_name, message = _extract_log_context(event, hint)
    if _is_healthcheck_access_log(logger_name, message):
        return None
    if _is_yfinance_crumb_error(logger_name, message):
        return None
    if _is_yfinance_noise_log(logger_name, message):
        return None
    if _is_yfinance_html_500_noise(logger_name, message):
        return None
    if _is_fastmcp_tool_validation_error(logger_name, message):
        return None
    if _is_expected_mcp_argument_noise(logger_name, message, event):
        return None
    return _sanitize_in_place(event)


def _before_breadcrumb(crumb: Breadcrumb, hint: Hint) -> Breadcrumb | None:
    del hint
    category = crumb.get("category")
    message = crumb.get("message")
    if isinstance(category, str) and isinstance(message, str):
        if _is_healthcheck_access_log(category, message):
            return None
    return _sanitize_in_place(crumb)


def _before_send_log(sentry_log: Log, hint: Hint) -> Log | None:
    logger_name, message = _extract_sentry_log_context(sentry_log, hint)
    if _is_healthcheck_access_log(logger_name, message):
        return None
    if _is_yfinance_crumb_error(logger_name, message):
        return None
    if _is_yfinance_noise_log(logger_name, message):
        return None
    if _is_yfinance_html_500_noise(logger_name, message):
        return None
    if _is_fastmcp_tool_validation_error(logger_name, message):
        return None
    if _is_expected_mcp_argument_noise(logger_name, message):
        return None
    return _sanitize_in_place(sentry_log)


def _before_send_transaction(event: Event, hint: Hint) -> Event | None:
    del hint

    transaction_name = event.get("transaction", "")
    if not isinstance(transaction_name, str) or "/mcp" not in transaction_name:
        return event

    spans = event.get("spans", [])
    if not isinstance(spans, list):
        spans = []

    for span in spans:
        if not isinstance(span, dict) or span.get("op") != "mcp.server":
            continue

        span_data = span.get("data", {})
        if not isinstance(span_data, dict):
            span_data = {}

        tool_name = span_data.get("mcp.tool.name", "")
        method_name = span_data.get("mcp.method.name", "")
        if isinstance(tool_name, str) and tool_name:
            method_prefix = (
                method_name
                if isinstance(method_name, str) and method_name
                else "tools/call"
            )
            event["transaction"] = f"{method_prefix} {tool_name}"
            transaction_info = event.get("transaction_info")
            if not isinstance(transaction_info, dict):
                transaction_info = {}
            transaction_info["source"] = "custom"
            event["transaction_info"] = transaction_info

        return event

    return None


def _resolve_release() -> str | None:
    try:
        release = _BUILD_VCS_REF_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        release = ""

    if release:
        return release

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    release = result.stdout.strip()
    return release or None


def init_sentry(
    service_name: str,
    enable_fastapi: bool = False,
    enable_sqlalchemy: bool = False,
    enable_httpx: bool = False,
    enable_mcp: bool = False,
    debug: bool | None = None,
) -> bool:
    """Initialize Sentry once per process."""
    global _enabled_integration_flags, _initialized

    requested_flags = {
        "fastapi": enable_fastapi,
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
    release = _resolve_release()
    log_event_level = logging.ERROR if settings.SENTRY_ENABLE_LOG_EVENTS else None

    integrations: list[Any] = [
        LoggingIntegration(level=logging.INFO, event_level=log_event_level)
    ]
    if effective_flags["fastapi"]:
        integrations.append(FastApiIntegration())
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

    enable_debug = debug if debug is not None else settings.SENTRY_DEBUG

    try:
        if _initialized:
            logger.info(
                "Reinitializing Sentry to add integrations: previous=%s requested=%s",
                sorted(
                    key
                    for key, enabled in _enabled_integration_flags.items()
                    if enabled
                ),
                sorted(key for key, enabled in effective_flags.items() if enabled),
            )
        _ = sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release,
            debug=enable_debug,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            profiles_sample_rate=settings.SENTRY_PROFILES_SAMPLE_RATE,
            send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
            integrations=integrations,
            before_send=_before_send,
            before_breadcrumb=_before_breadcrumb,
            before_send_log=_before_send_log,
            before_send_transaction=_before_send_transaction,
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


_MAX_STRING_LENGTH = 1024
_MAX_SEQUENCE_LENGTH = 25
_MAX_DICT_KEYS = 25
_TRUNCATED_MARKER = "...[truncated]"


def _truncate_for_sentry(value: Any) -> Any:
    """Truncate oversized values for Sentry structured context.

    Limits:
        * Strings – first 1 024 characters.
        * Lists / tuples – first 25 elements.
        * Dicts – first 25 keys.

    A visible marker is appended whenever a value is truncated.
    The function is pure – the original *value* is never mutated.
    """
    if isinstance(value, str):
        if len(value) > _MAX_STRING_LENGTH:
            return value[:_MAX_STRING_LENGTH] + _TRUNCATED_MARKER
        return value

    if isinstance(value, dict):
        keys = list(value.keys())
        if len(keys) > _MAX_DICT_KEYS:
            result = {k: _truncate_for_sentry(value[k]) for k in keys[:_MAX_DICT_KEYS]}
            result[_TRUNCATED_MARKER] = f"{len(keys) - _MAX_DICT_KEYS} more keys"
            return result
        return {k: _truncate_for_sentry(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        is_tuple = isinstance(value, tuple)
        if len(value) > _MAX_SEQUENCE_LENGTH:
            items: list[Any] = [
                _truncate_for_sentry(item) for item in value[:_MAX_SEQUENCE_LENGTH]
            ]
            items.append(
                f"...[truncated: {len(value) - _MAX_SEQUENCE_LENGTH} more items]"
            )
            return tuple(items) if is_tuple else items
        items = [_truncate_for_sentry(item) for item in value]
        return tuple(items) if is_tuple else items

    return value


def build_mcp_tool_call_context(
    tool_name: str, arguments: dict[str, Any] | None
) -> dict[str, Any]:
    """Build a sanitized, truncated structured context for an MCP tool call.

    Reuses the project-wide sensitive-key filter and applies size limits
    so the payload stays within Sentry's recommended context size.

    Returns a dict with exactly two keys: ``tool_name`` and ``arguments``.
    """
    safe_args = _truncate_for_sentry(_sanitize_in_place(copy.deepcopy(arguments or {})))
    return {
        "tool_name": tool_name,
        "arguments": safe_args,
    }


def get_mcp_http_scopes() -> (
    tuple[sentry_sdk.Scope | None, sentry_sdk.Scope | None] | None
):
    if _mcp_request_ctx is None:
        return None

    try:
        ctx = _mcp_request_ctx.get()
    except LookupError:
        return None

    if ctx is None or not hasattr(ctx, "request") or ctx.request is None:
        return None

    request_scope = getattr(ctx.request, "scope", None)
    if not isinstance(request_scope, dict) or request_scope.get("type") != "http":
        return None

    state = request_scope.get("state")
    if not isinstance(state, dict):
        return None

    return (
        state.get("sentry_sdk.isolation_scope"),
        state.get("sentry_sdk.current_scope"),
    )


def enrich_mcp_tool_call_scope(
    scope: sentry_sdk.Scope, tool_name: str, arguments: dict[str, Any] | None
) -> None:
    scope.set_tag("mcp.tool.name", tool_name)
    scope.set_context(
        "mcp_tool_call",
        build_mcp_tool_call_context(tool_name, arguments),
    )


def capture_exception(exc: BaseException, **context: Any) -> None:
    """Capture an exception with additional context if Sentry is initialized."""
    if not _initialized:
        return

    try:
        scope_factory = getattr(sentry_sdk, "new_scope", sentry_sdk.push_scope)
        with scope_factory() as scope:
            for key, value in context.items():
                scope.set_extra(str(key), _sanitize_in_place(value, str(key)))
            sentry_sdk.capture_exception(exc)
    except Exception:
        logger.exception("Failed to capture exception in Sentry")
