"""Shared Sentry initialization and capture helpers."""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import sentry_sdk
from sentry_sdk.consts import SPANSTATUS
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
    if _is_fastmcp_tool_validation_error(logger_name, message):
        return None
    if _is_expected_mcp_argument_noise(logger_name, message):
        return None
    return _sanitize_in_place(sentry_log)


def _before_send_transaction(event: Event, hint: Hint) -> Event | None:
    del hint

    transaction_name = event.get("transaction", "")
    if not isinstance(transaction_name, str):
        return event
    is_mcp_transport_transaction = "/mcp" in transaction_name

    spans = event.get("spans", [])
    if not isinstance(spans, list):
        spans = []

    found_mcp_span = False
    transaction_renamed = False
    for span in spans:
        if not isinstance(span, dict) or span.get("op") != "mcp.server":
            continue
        found_mcp_span = True

        span_data = span.get("data", {})
        if not isinstance(span_data, dict):
            span_data = {}
        _scrub_mcp_span_high_cardinality_data(span_data)
        span["data"] = span_data

        tool_name = span_data.get("mcp.tool.name", "")
        method_name = span_data.get("mcp.method.name", "")
        if not transaction_renamed and isinstance(tool_name, str) and tool_name:
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
            transaction_renamed = True

    if found_mcp_span:
        return event
    return None if is_mcp_transport_transaction else event


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


_MISSING = object()

_FRESHNESS_FIELD_NAMES = (
    "data_state",
    "derived_as_of",
    "fetched_at",
    "data_age_seconds",
    "cache_hit",
    "fallback_source",
    "provider_provenance",
)
_FRESHNESS_DATA_STATES = frozenset({"fresh", "stale", "degraded", "missing"})
_PROVENANCE_FIELD_NAMES = frozenset(
    {"provider", "served_by", "mode", "status", "error_code"}
)
_PROVENANCE_MODES = frozenset({"live", "cache", "fallback", "mixed", "none"})
_PROVENANCE_STATUSES = frozenset({"ok", "empty", "error", "unavailable"})

_SYMBOL_FIELD_NAMES = frozenset(
    {
        "crypto_instrument_id",
        "exclude_symbols",
        "expected_execution_symbol",
        "expected_signal_symbol",
        "held_symbols",
        "instrument_id",
        "market_symbol",
        "normalized_symbol",
        "position_symbol",
        "query_symbol",
        "stock_code",
        "symbol",
        "symbol_to_ids",
        "symbols",
        "stock_symbol",
        "stock_symbols",
        "ticker",
        "tickers",
        "upbit_symbol",
        "yf_ticker",
    }
)
_SYMBOL_REDACTION = "[Filtered: high-cardinality symbol]"
_MAX_OBSERVABILITY_VALUE_LENGTH = 200
_MAX_OBSERVABILITY_WALK_NODES = 500
_MAX_SYMBOL_VALUES = 1000
_STABLE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")

_FUNNEL_TOOL_STAGES: dict[str, frozenset[str]] = {
    "bootstrap": frozenset({"get_operating_briefing"}),
    "lane": frozenset({"route_request", "get_trading_policy"}),
    "evidence": frozenset(
        {
            "analysis_bundle_create",
            "analysis_bundle_get",
            "analysis_artifact_get",
            "analysis_artifact_list",
            "analyze_portfolio",
            "analyze_stock",
            "analyze_stock_batch",
            "get_analyst_consensus",
            "get_correlation",
            "get_disclosures",
            "get_fx_rate",
            "get_intraday_investor_flow",
            "get_market_index",
            "get_market_news",
            "get_momentum_candidates",
            "get_news",
            "get_ohlcv",
            "get_orderbook",
            "get_quote",
            "get_sector_peers",
            "get_top_stocks",
            "investment_report_get_hermes_context",
            "investment_report_prepare_bundle",
            "investment_report_prepare_intraday_context",
            "market_quote_snapshot_ensure",
            "market_quote_snapshot_latest",
            "screen_stocks",
            "screen_stocks_snapshot",
            "sell_ladder_fill_preview",
            "session_context_get_recent",
            "toss_get_positions",
        }
    ),
    "verdict": frozenset(
        {
            "investment_report_activate_watch",
            "investment_report_add_items",
            "investment_report_create",
            "investment_report_create_from_hermes_composition",
            "investment_report_decide_item",
            "investment_report_generate_from_bundle",
            "investment_report_set_status",
        }
    ),
    "artifact": frozenset(
        {
            "analysis_artifact_save",
            "investment_stage_artifacts_ingest_from_hermes",
        }
    ),
    "proposal": frozenset({"order_proposal_create"}),
    "fill": frozenset(
        {
            "alpaca_paper_list_fills",
            "execution_ledger_fill_events_list_recent",
            "kis_mock_reconciliation_run",
            "paper_execution_reconcile",
        }
    ),
    "retrospective": frozenset({"save_trade_retrospective"}),
}


def resolve_mcp_funnel_stage(tool_name: str) -> str:
    """Return a bounded stage label for operator-funnel measurement."""
    for stage, tool_names in _FUNNEL_TOOL_STAGES.items():
        if tool_name in tool_names:
            return stage
    if tool_name.endswith(("_reconcile_orders", "_reconciliation_run")):
        return "fill"
    return "other"


def _clean_observability_value(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float | UUID):
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_OBSERVABILITY_VALUE_LENGTH]


def _stable_code(value: Any) -> str | None:
    cleaned = _clean_observability_value(value)
    if cleaned is None or _STABLE_CODE_RE.fullmatch(cleaned) is None:
        return None
    return cleaned


def _walk_mapping_values(
    payload: Mapping[str, Any] | None,
) -> list[tuple[str, Any]]:
    if payload is None:
        return []

    found: list[tuple[str, Any]] = []
    stack: list[tuple[Any, int]] = [(payload, 0)]
    seen: set[int] = set()
    visited = 0
    while stack and visited < _MAX_OBSERVABILITY_WALK_NODES:
        value, depth = stack.pop()
        if isinstance(value, Mapping):
            value_id = id(value)
            if value_id in seen:
                continue
            seen.add(value_id)
            for key, nested in value.items():
                visited += 1
                key_str = str(key)
                found.append((key_str, nested))
                if depth < 4 and isinstance(nested, Mapping | list | tuple):
                    stack.append((nested, depth + 1))
                if visited >= _MAX_OBSERVABILITY_WALK_NODES:
                    break
        elif isinstance(value, list | tuple):
            value_id = id(value)
            if value_id in seen:
                continue
            seen.add(value_id)
            for nested in value[:_MAX_SEQUENCE_LENGTH]:
                visited += 1
                if depth < 4 and isinstance(nested, Mapping | list | tuple):
                    stack.append((nested, depth + 1))
                if visited >= _MAX_OBSERVABILITY_WALK_NODES:
                    break
    return found


def _find_unique_scalar(
    payload: Mapping[str, Any] | None,
    field_names: tuple[str, ...],
) -> str | None:
    if payload is None:
        return None

    for field_name in field_names:
        if field_name in payload:
            direct = _clean_observability_value(payload.get(field_name))
            if direct is not None:
                return direct

    matches = {
        cleaned
        for key, value in _walk_mapping_values(payload)
        if key in field_names
        if (cleaned := _clean_observability_value(value)) is not None
    }
    if len(matches) == 1:
        return next(iter(matches))
    return None


def _first_unique_scalar(
    payloads: tuple[Mapping[str, Any] | None, ...],
    field_names: tuple[str, ...],
) -> str | None:
    for payload in payloads:
        value = _find_unique_scalar(payload, field_names)
        if value is not None:
            return value
    return None


def _valid_uuid(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        UUID(value)
    except (ValueError, AttributeError):
        return None
    return value


def _extract_lineage_identifiers(
    arguments: Mapping[str, Any],
    envelope: Mapping[str, Any] | None,
    transport_session_id: str | None,
) -> tuple[dict[str, str | None], str]:
    operator_session = _first_unique_scalar(
        (arguments, envelope),
        ("operator_session",),
    )
    operator_session_source = "operator_session"
    if operator_session is None:
        operator_session = _first_unique_scalar(
            (arguments, envelope),
            ("session_label",),
        )
        operator_session_source = "session_label"
    if operator_session is None:
        operator_session = _clean_observability_value(transport_session_id)
        operator_session_source = (
            "mcp_session" if operator_session is not None else "none"
        )

    proposal_uuid = _first_unique_scalar(
        (envelope, arguments),
        ("proposal_uuid",),
    )
    if proposal_uuid is None:
        proposal_uuid = _valid_uuid(
            _first_unique_scalar((envelope, arguments), ("proposal_id",))
        )
    artifact_uuid = _first_unique_scalar(
        (envelope, arguments),
        ("artifact_uuid",),
    )
    if artifact_uuid is None:
        artifact_uuid = _valid_uuid(
            _first_unique_scalar((envelope, arguments), ("artifact_id",))
        )

    identifiers = {
        "operator_session": operator_session,
        "analysis_run_id": _first_unique_scalar(
            (arguments, envelope),
            ("analysis_run_id",),
        ),
        "correlation_id": _first_unique_scalar(
            (arguments, envelope),
            ("correlation_id",),
        ),
        "lane": _first_unique_scalar((envelope, arguments), ("lane",)),
        "verdict": _first_unique_scalar((envelope, arguments), ("verdict",)),
        "report_uuid": _first_unique_scalar(
            (envelope, arguments),
            ("report_uuid",),
        ),
        "artifact_uuid": artifact_uuid,
        "proposal_uuid": proposal_uuid,
    }
    return identifiers, operator_session_source


def _extract_market(
    arguments: Mapping[str, Any],
    envelope: Mapping[str, Any] | None,
) -> str:
    market = _first_unique_scalar((arguments, envelope), ("market",))
    stable_market = _stable_code(market)
    return stable_market.lower() if stable_market is not None else "unknown"


def _collect_symbol_values(payload: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()

    def add_value(value: Any) -> None:
        if len(values) >= _MAX_SYMBOL_VALUES:
            return
        if isinstance(value, str):
            cleaned = value.strip().upper()
            if cleaned:
                values.add(cleaned)
        elif isinstance(value, list | tuple | set | frozenset):
            for nested in value:
                add_value(nested)
                if len(values) >= _MAX_SYMBOL_VALUES:
                    break

    for key, value in payload.items():
        if str(key).lower() in _SYMBOL_FIELD_NAMES:
            add_value(value)
    for key, value in _walk_mapping_values(payload):
        if key.lower() in _SYMBOL_FIELD_NAMES:
            add_value(value)
    return values


def _symbol_count_bucket(count: int) -> str:
    if count == 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 10:
        return "2-10"
    if count <= 50:
        return "11-50"
    return "51+"


def _build_symbol_summary(arguments: Mapping[str, Any]) -> dict[str, Any]:
    count = len(_collect_symbol_values(arguments))
    return {
        "mode": "none" if count == 0 else "single" if count == 1 else "batch",
        "count": count,
        "count_bucket": _symbol_count_bucket(count),
    }


def _valid_timestamp(value: Any) -> tuple[str | None, bool]:
    if value is None:
        return None, True
    if not isinstance(value, str):
        return None, False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, False
    return value, True


def _valid_contract_string(
    value: Any,
    *,
    nullable: bool,
) -> tuple[str | None, bool]:
    if value is None:
        return None, nullable
    if not isinstance(value, str):
        return None, False
    return value[:_MAX_OBSERVABILITY_VALUE_LENGTH], True


def _valid_provider_provenance(
    value: Any,
) -> tuple[list[dict[str, Any]] | None, bool]:
    if not isinstance(value, list):
        return None, False

    validated: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping) or set(entry) != _PROVENANCE_FIELD_NAMES:
            return None, False
        provider, provider_valid = _valid_contract_string(
            entry.get("provider"),
            nullable=False,
        )
        served_by, served_by_valid = _valid_contract_string(
            entry.get("served_by"),
            nullable=True,
        )
        mode = entry.get("mode")
        status = entry.get("status")
        error_code, error_code_valid = _valid_contract_string(
            entry.get("error_code"),
            nullable=True,
        )
        if (
            not provider_valid
            or not served_by_valid
            or not isinstance(mode, str)
            or mode not in _PROVENANCE_MODES
            or not isinstance(status, str)
            or status not in _PROVENANCE_STATUSES
            or not error_code_valid
        ):
            return None, False
        validated.append(
            {
                "provider": provider,
                "served_by": served_by,
                "mode": mode,
                "status": status,
                "error_code": error_code,
            }
        )
    return validated, True


def _build_freshness_observation(
    envelope: Mapping[str, Any] | None,
) -> dict[str, Any]:
    envelope = envelope or {}
    present_fields = [
        field_name for field_name in _FRESHNESS_FIELD_NAMES if field_name in envelope
    ]
    validity: dict[str, bool] = {}

    raw_data_state = envelope.get("data_state", _MISSING)
    data_state_valid = (
        isinstance(raw_data_state, str) and raw_data_state in _FRESHNESS_DATA_STATES
    )
    data_state = raw_data_state if data_state_valid else "unknown"
    if raw_data_state is not _MISSING:
        validity["data_state"] = data_state_valid

    derived_as_of = None
    if "derived_as_of" in envelope:
        derived_as_of, validity["derived_as_of"] = _valid_timestamp(
            envelope["derived_as_of"]
        )

    fetched_at = None
    if "fetched_at" in envelope:
        fetched_at, validity["fetched_at"] = _valid_timestamp(envelope["fetched_at"])

    data_age_seconds: int | float | None = None
    if "data_age_seconds" in envelope:
        raw_age = envelope["data_age_seconds"]
        age_valid = (
            raw_age is None
            or (
                isinstance(raw_age, int)
                and not isinstance(raw_age, bool)
                and raw_age >= 0
            )
            or (isinstance(raw_age, float) and math.isfinite(raw_age) and raw_age >= 0)
        )
        validity["data_age_seconds"] = age_valid
        if age_valid:
            data_age_seconds = raw_age

    cache_hit: bool | None = None
    if "cache_hit" in envelope:
        raw_cache_hit = envelope["cache_hit"]
        validity["cache_hit"] = isinstance(raw_cache_hit, bool)
        if isinstance(raw_cache_hit, bool):
            cache_hit = raw_cache_hit

    fallback_source = None
    if "fallback_source" in envelope:
        fallback_source, validity["fallback_source"] = _valid_contract_string(
            envelope["fallback_source"],
            nullable=True,
        )

    provider_provenance = None
    if "provider_provenance" in envelope:
        (
            provider_provenance,
            validity["provider_provenance"],
        ) = _valid_provider_provenance(envelope["provider_provenance"])

    if not present_fields:
        contract_status = "absent"
    elif not all(validity.values()):
        contract_status = "invalid"
    elif len(present_fields) == len(_FRESHNESS_FIELD_NAMES):
        contract_status = "complete"
    else:
        contract_status = "partial"

    return {
        "contract_status": contract_status,
        "present_fields": present_fields,
        "missing_fields": [
            field_name
            for field_name in _FRESHNESS_FIELD_NAMES
            if field_name not in envelope
        ],
        "data_state": data_state,
        "derived_as_of": derived_as_of,
        "fetched_at": fetched_at,
        "data_age_seconds": data_age_seconds,
        "cache_hit": cache_hit,
        "fallback_source": fallback_source,
        "provider_provenance": provider_provenance,
    }


def _unwrap_result_envelope(value: Mapping[str, Any]) -> Mapping[str, Any]:
    wrapped = value.get("result")
    if len(value) == 1 and isinstance(wrapped, Mapping):
        return wrapped
    return value


def _mapping_from_json_text(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, str) or not value.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, Mapping):
        return _unwrap_result_envelope(parsed)
    return None


def extract_mcp_result_envelope(result: Any) -> Mapping[str, Any] | None:
    """Extract the actual FastMCP structured envelope.

    Production middleware receives ``ToolResult`` rather than the raw dict used
    by many unit fixtures. Wire ``CallToolResult`` and JSON text content are
    accepted as defensive compatibility paths.
    """
    if isinstance(result, Mapping):
        structured = result.get("structuredContent")
        if isinstance(structured, Mapping) and (
            "content" in result or "isError" in result
        ):
            return _unwrap_result_envelope(structured)
        return _unwrap_result_envelope(result)

    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        if isinstance(structured, Mapping):
            return _unwrap_result_envelope(structured)

    for attribute_name in ("structured_content", "structuredContent"):
        structured = getattr(result, attribute_name, None)
        if isinstance(structured, Mapping):
            return _unwrap_result_envelope(structured)

    content = getattr(result, "content", None)
    if isinstance(content, list | tuple):
        for item in content:
            text_value = getattr(item, "text", None)
            if text_value is None and isinstance(item, Mapping):
                text_value = item.get("text")
            parsed = _mapping_from_json_text(text_value)
            if parsed is not None:
                return parsed
    return _mapping_from_json_text(content)


def _result_is_protocol_error(result: Any) -> bool:
    if isinstance(result, Mapping):
        return result.get("isError") is True or result.get("is_error") is True
    return (
        getattr(result, "isError", False) is True
        or getattr(result, "is_error", False) is True
    )


def _has_true_flag(
    envelope: Mapping[str, Any] | None,
    field_names: frozenset[str],
) -> bool:
    if envelope is None:
        return False
    return any(
        key in field_names and value is True
        for key, value in _walk_mapping_values(envelope)
    )


def _extract_error_code(
    envelope: Mapping[str, Any] | None,
    freshness: Mapping[str, Any],
    semantic_success: bool,
) -> str | None:
    if envelope is not None:
        for field_name in ("error_code", "error_type"):
            code = _stable_code(envelope.get(field_name))
            if code is not None:
                return code

        raw_error = envelope.get("error")
        if isinstance(raw_error, Mapping):
            code = _stable_code(raw_error.get("code"))
        else:
            code = _stable_code(raw_error)
        if code is not None:
            return code

    provenance = freshness.get("provider_provenance")
    if isinstance(provenance, list):
        provider_codes = {
            code
            for entry in provenance
            if isinstance(entry, Mapping)
            if (code := _stable_code(entry.get("error_code"))) is not None
        }
        if len(provider_codes) == 1:
            return next(iter(provider_codes))
        if len(provider_codes) > 1:
            return "multiple_provider_errors"

    if semantic_success:
        return None
    data_state = freshness.get("data_state")
    if data_state in {"stale", "degraded", "missing"}:
        return f"{data_state}_data"
    if _has_true_flag(envelope, frozenset({"stale", "is_stale"})):
        return "stale_data"
    if _has_true_flag(envelope, frozenset({"degraded"})):
        return "degraded_data"
    return "semantic_failure"


def _semantic_success(
    envelope: Mapping[str, Any] | None,
    freshness: Mapping[str, Any],
    protocol_error: bool,
) -> bool:
    if protocol_error:
        return False
    explicit_success = envelope.get("success") if envelope is not None else None
    if explicit_success is False:
        return False
    if freshness.get("data_state") in {"stale", "degraded", "missing"}:
        return False
    if _has_true_flag(envelope, frozenset({"stale", "is_stale", "degraded"})):
        return False
    if (
        explicit_success is not True
        and envelope is not None
        and any(
            envelope.get(field_name) is not None
            for field_name in ("error", "error_code")
        )
    ):
        return False
    return explicit_success if isinstance(explicit_success, bool) else True


def _exception_span_status(exception: BaseException) -> str:
    exception_name = type(exception).__name__
    if exception_name in {"CancelledError", "KeyboardInterrupt"}:
        return SPANSTATUS.CANCELLED
    if isinstance(exception, TimeoutError) or "Timeout" in exception_name:
        return SPANSTATUS.DEADLINE_EXCEEDED
    if isinstance(exception, ValueError) or "ValidationError" in exception_name:
        return SPANSTATUS.INVALID_ARGUMENT
    return SPANSTATUS.INTERNAL_ERROR


def _data_age_bucket(value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "unknown"
    if value < 1:
        return "<1s"
    if value < 60:
        return "1-59s"
    if value < 300:
        return "1-4m"
    if value < 900:
        return "5-14m"
    if value < 3600:
        return "15-59m"
    if value < 21600:
        return "1-5h"
    if value < 86400:
        return "6-23h"
    return "24h+"


def build_mcp_tool_observation(
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    *,
    result: Any = _MISSING,
    exception: BaseException | None = None,
    caller_agent_id: str | None = None,
    caller_source: str = "none",
    transport_session_id: str | None = None,
) -> dict[str, Any]:
    """Build bounded operator and semantic observations for one MCP call."""
    safe_arguments = arguments or {}
    envelope = (
        extract_mcp_result_envelope(result)
        if result is not _MISSING and exception is None
        else None
    )
    freshness = _build_freshness_observation(envelope)
    identifiers, operator_session_source = _extract_lineage_identifiers(
        safe_arguments,
        envelope,
        transport_session_id,
    )

    completed = result is not _MISSING or exception is not None
    semantic_success: bool | None = None
    error_code: str | None = None
    span_status: str | None = None
    if exception is not None:
        semantic_success = False
        error_code = type(exception).__name__
        span_status = _exception_span_status(exception)
    elif completed:
        semantic_success = _semantic_success(
            envelope,
            freshness,
            _result_is_protocol_error(result),
        )
        error_code = _extract_error_code(
            envelope,
            freshness,
            semantic_success,
        )
        span_status = (
            SPANSTATUS.OK if semantic_success else SPANSTATUS.FAILED_PRECONDITION
        )

    return {
        "tool_name": tool_name,
        "consumer": _clean_observability_value(caller_agent_id) or "unknown",
        "caller_source": _stable_code(caller_source) or "none",
        **identifiers,
        "operator_session_source": operator_session_source,
        "market": _extract_market(safe_arguments, envelope),
        "funnel_stage": resolve_mcp_funnel_stage(tool_name),
        "symbol": _build_symbol_summary(safe_arguments),
        "semantic_success": semantic_success,
        "error_code": error_code,
        "span_status": span_status,
        "freshness": freshness,
    }


def _redact_symbol_fields(value: Any, parent_key: str | None = None) -> Any:
    if parent_key is not None and parent_key.lower() in _SYMBOL_FIELD_NAMES:
        return _SYMBOL_REDACTION
    if isinstance(value, Mapping):
        return {
            key: _redact_symbol_fields(nested, str(key))
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact_symbol_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_symbol_fields(item) for item in value)
    return value


def _safe_mcp_span_argument(key: str, value: Any) -> Any:
    copied = copy.deepcopy(value)
    sanitized = _sanitize_in_place(copied, key)
    redacted = _redact_symbol_fields(sanitized, key)
    return _truncate_for_sentry(redacted)


def _redact_serialized_symbol_fields(value: Any, parent_key: str | None) -> Any:
    if parent_key is not None and parent_key.lower() in _SYMBOL_FIELD_NAMES:
        return _SYMBOL_REDACTION
    if not isinstance(value, str):
        return _redact_symbol_fields(value, parent_key)
    stripped = value.lstrip()
    if not stripped.startswith(("{", "[")):
        return value
    if len(value) > 1_000_000:
        return "[Filtered: oversized MCP payload]"
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    redacted = _redact_symbol_fields(parsed, parent_key)
    return json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)


def _scrub_mcp_span_high_cardinality_data(span_data: dict[str, Any]) -> None:
    """Remove raw symbols from Sentry's indexed MCP span attributes."""
    for key, value in list(span_data.items()):
        if key.startswith("mcp.request.argument."):
            argument_name = key.removeprefix("mcp.request.argument.")
            span_data[key] = _redact_serialized_symbol_fields(
                value,
                argument_name,
            )
        elif key == "mcp.tool.result.content":
            span_data[key] = _redact_serialized_symbol_fields(value, None)


def _set_scope_and_span_tag(
    scope: sentry_sdk.Scope,
    span: Any,
    key: str,
    value: Any,
) -> None:
    tag_value = str(value)
    scope.set_tag(key, tag_value)
    set_tag = getattr(span, "set_tag", None)
    if callable(set_tag):
        set_tag(key, tag_value)


def _set_span_data(span: Any, key: str, value: Any) -> None:
    set_data = getattr(span, "set_data", None)
    if callable(set_data):
        set_data(key, value)


def _apply_mcp_tool_observation(
    scope: sentry_sdk.Scope,
    span: Any,
    observation: Mapping[str, Any],
) -> None:
    scope.set_context(
        "mcp_tool_observability",
        _truncate_for_sentry(dict(observation)),
    )

    symbol = observation["symbol"]
    freshness = observation["freshness"]
    base_tags = {
        "mcp.consumer": observation["consumer"],
        "mcp.caller.source": observation["caller_source"],
        "mcp.operator_session": observation["operator_session"] or "unknown",
        "mcp.operator_session.source": observation["operator_session_source"],
        "mcp.market": observation["market"],
        "mcp.funnel.stage": observation["funnel_stage"],
        "mcp.symbol.mode": symbol["mode"],
        "mcp.symbol.count_bucket": symbol["count_bucket"],
        "mcp.data_state": freshness["data_state"],
        "mcp.cache_hit": (
            str(freshness["cache_hit"]).lower()
            if isinstance(freshness["cache_hit"], bool)
            else "unknown"
        ),
        "mcp.freshness.contract": freshness["contract_status"],
        "mcp.data_age.bucket": _data_age_bucket(freshness["data_age_seconds"]),
    }
    present_fields = set(freshness["present_fields"])
    if "fallback_source" not in present_fields:
        base_tags["mcp.fallback_source"] = "unknown"
    elif freshness["fallback_source"] is None:
        base_tags["mcp.fallback_source"] = "none"
    else:
        base_tags["mcp.fallback_source"] = freshness["fallback_source"]

    for key, value in base_tags.items():
        _set_scope_and_span_tag(scope, span, key, value)

    for field_name in (
        "analysis_run_id",
        "correlation_id",
        "lane",
        "verdict",
        "report_uuid",
        "artifact_uuid",
        "proposal_uuid",
    ):
        value = observation[field_name]
        if value is not None:
            _set_scope_and_span_tag(scope, span, f"mcp.{field_name}", value)

    semantic_success = observation["semantic_success"]
    if semantic_success is not None:
        _set_scope_and_span_tag(
            scope,
            span,
            "mcp.semantic_success",
            str(semantic_success).lower(),
        )
        _set_scope_and_span_tag(
            scope,
            span,
            "mcp.error_code",
            observation["error_code"] or "none",
        )

    for field_name in (
        "consumer",
        "operator_session",
        "analysis_run_id",
        "correlation_id",
        "lane",
        "verdict",
        "report_uuid",
        "artifact_uuid",
        "proposal_uuid",
        "market",
        "funnel_stage",
    ):
        value = observation[field_name]
        if value is not None:
            _set_span_data(span, f"mcp.{field_name}", value)
    _set_span_data(span, "mcp.caller.source", observation["caller_source"])
    _set_span_data(span, "mcp.symbol.mode", symbol["mode"])
    _set_span_data(span, "mcp.symbol.count", symbol["count"])
    _set_span_data(span, "mcp.symbol.count_bucket", symbol["count_bucket"])
    _set_span_data(span, "mcp.data_state", freshness["data_state"])
    _set_span_data(
        span,
        "mcp.cache_hit",
        freshness["cache_hit"]
        if isinstance(freshness["cache_hit"], bool)
        else "unknown",
    )
    _set_span_data(
        span,
        "mcp.freshness.contract",
        freshness["contract_status"],
    )
    _set_span_data(
        span,
        "mcp.freshness.present_fields",
        freshness["present_fields"],
    )
    for field_name in (
        "derived_as_of",
        "fetched_at",
        "data_age_seconds",
        "fallback_source",
        "provider_provenance",
    ):
        if field_name in present_fields:
            _set_span_data(span, f"mcp.{field_name}", freshness[field_name])

    if semantic_success is not None:
        _set_span_data(span, "mcp.semantic_success", semantic_success)
        _set_span_data(span, "mcp.error_code", observation["error_code"])
        set_status = getattr(span, "set_status", None)
        if callable(set_status):
            set_status(observation["span_status"])


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
    scope: sentry_sdk.Scope,
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    caller_agent_id: str | None = None,
    caller_source: str = "none",
    transport_session_id: str | None = None,
    span: Any = None,
) -> None:
    if span is None:
        span = sentry_sdk.get_current_span()
    scope.set_tag("mcp.tool.name", tool_name)
    observation = build_mcp_tool_observation(
        tool_name,
        arguments,
        caller_agent_id=caller_agent_id,
        caller_source=caller_source,
        transport_session_id=transport_session_id,
    )
    _apply_mcp_tool_observation(scope, span, observation)
    scope.set_context(
        "mcp_tool_call",
        build_mcp_tool_call_context(tool_name, arguments),
    )
    for key, value in (arguments or {}).items():
        _set_span_data(
            span,
            f"mcp.request.argument.{key}",
            _safe_mcp_span_argument(str(key), value),
        )


def record_mcp_tool_call_result(
    scope: sentry_sdk.Scope,
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    result: Any = _MISSING,
    exception: BaseException | None = None,
    caller_agent_id: str | None = None,
    caller_source: str = "none",
    transport_session_id: str | None = None,
    span: Any = None,
) -> None:
    """Attach semantic result, lineage, freshness, and span status."""
    if span is None:
        span = sentry_sdk.get_current_span()
    observation = build_mcp_tool_observation(
        tool_name,
        arguments,
        result=result,
        exception=exception,
        caller_agent_id=caller_agent_id,
        caller_source=caller_source,
        transport_session_id=transport_session_id,
    )
    _apply_mcp_tool_observation(scope, span, observation)


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
