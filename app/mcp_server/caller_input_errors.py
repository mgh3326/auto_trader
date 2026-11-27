"""hk 6805/hk 7081: classify MCP caller argument errors vs server faults.

MCP callers passing bad arguments used to reach Sentry twice per call:
FastMCP logs ``Error calling tool '<name>'`` with the original exception
and then re-raises it wrapped in ``ToolError``, which the sentry-sdk MCP
integration captures again at the ``call_tool`` boundary. Caller input
mistakes are not server faults, so they are counted here instead: one
warning log plus one ``mcp_metric`` counter sample per call
(Loki-countable, same convention as ``app.monitoring.execution_metrics``).

hk 7081 narrowed the caller-input set to a closed list. It is
``CallerInputError`` — raised only at genuine argument-validation sites —
plus the framework argument-validation errors (``pydantic.ValidationError``
and ``fastmcp.exceptions.ValidationError``). Plain ``ValueError`` is a
server fault again: ~324 ``raise ValueError`` sites under
``app/mcp_server/`` include real internal failures, and every one of them
lost its Sentry event while the whole type was treated as caller input.
``app.monitoring.sentry`` derives its serialized exception-type sets from
``CALLER_INPUT_ERROR_TYPES``/``MCP_ERROR_WRAPPER_TYPES`` below, so the live
classifier and the event-drop path share one definition.
"""

from __future__ import annotations

import logging
from typing import Any

import pydantic
from fastmcp.exceptions import FastMCPError, ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from mcp.shared.exceptions import McpError

from app.mcp_server.timeout_middleware import McpToolDeadlineExceeded
from app.mcp_server.tool_call_log_middleware import sanitize_log_field
from app.services.domain_errors import CallerInputError

logger = logging.getLogger(__name__)


# The closed caller-input set. ``app.monitoring.sentry`` derives its
# serialized exception-type names from these tuples — do not hand-maintain a
# second list there.
CALLER_INPUT_ERROR_TYPES: tuple[type[BaseException], ...] = (
    CallerInputError,
    pydantic.ValidationError,
    FastMCPValidationError,
)

# Transparent wrappers FastMCP/MCP add around whatever a tool raised; they
# never decide the caller-input class by themselves. ``McpToolDeadlineExceeded``
# is listed for its serialized name even though it is a ``ToolError`` subclass.
MCP_ERROR_WRAPPER_TYPES: tuple[type[BaseException], ...] = (
    ToolError,
    FastMCPError,
    McpError,
    McpToolDeadlineExceeded,
)

CALLER_INPUT_ERROR_TYPE_NAMES = frozenset(t.__name__ for t in CALLER_INPUT_ERROR_TYPES)
MCP_ERROR_WRAPPER_TYPE_NAMES = frozenset(t.__name__ for t in MCP_ERROR_WRAPPER_TYPES)

_CALLER_INPUT_MARKER_ATTR = "_mcp_caller_input_error"


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Walk ``__cause__``/``__context__`` links, deduplicated, outermost first."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def is_mcp_caller_input_error(exc: BaseException | None) -> bool:
    """True when every non-wrapper link in the chain is a caller-input type.

    ``ToolError``/``FastMCPError``/``McpError``/``McpToolDeadlineExceeded``
    are transparent wrappers FastMCP adds around whatever the tool raised,
    so they are skipped. A single non-wrapper, non-caller-input exception
    anywhere in the chain (a plain ``ValueError``, a ``RuntimeError``, an
    ``httpx`` error, a DB error) means a server fault and returns False —
    conservative: ambiguous chains stay on the Sentry path.
    """
    if exc is None:
        return False
    saw_input = False
    for link in _exception_chain(exc):
        if isinstance(link, CALLER_INPUT_ERROR_TYPES):
            saw_input = True
        elif isinstance(link, MCP_ERROR_WRAPPER_TYPES):
            continue
        else:
            return False
    return saw_input


def has_mcp_error_wrapper(exc: BaseException | None) -> bool:
    """True when the chain contains an MCP/FastMCP wrapper exception."""
    if exc is None:
        return False
    return any(
        isinstance(link, MCP_ERROR_WRAPPER_TYPES) for link in _exception_chain(exc)
    )


def mark_mcp_caller_input_error(exc: BaseException) -> None:
    """Tag ``exc`` and its chain so the Sentry drop path recognizes it.

    The ``mcp.tool.name`` tag / ``mcp_tool_call`` context only exist on the
    streamable-http request scope; on stdio/SSE the middleware enriches a
    temporary isolation scope that is gone by the time the SDK captures the
    exception on the ambient scope. The marker rides on the exception object
    itself, so ``app.monitoring.sentry`` drops exactly the failures this
    module counted, on every transport.
    """
    for link in _exception_chain(exc):
        try:
            setattr(link, _CALLER_INPUT_MARKER_ATTR, True)
        except Exception:  # noqa: BLE001 - marking must never break the call
            pass


def is_marked_mcp_caller_input_error(exc: BaseException | None) -> bool:
    """True when ``exc``'s chain carries the caller-input marker."""
    if exc is None:
        return False
    return any(
        getattr(link, _CALLER_INPUT_MARKER_ATTR, False) is True
        for link in _exception_chain(exc)
    )


def record_mcp_caller_input_error(tool_name: Any, exc: BaseException) -> None:
    """Emit the warning log and counter sample for one caller-input failure.

    Fails open like every other telemetry path: a logging failure must never
    change the tool outcome.
    """
    try:
        safe_tool = sanitize_log_field(tool_name)
        chain = _exception_chain(exc)
        error_type = type(chain[-1]).__name__ if chain else type(exc).__name__
        logger.warning(
            "mcp.tool.caller_input_error tool=%s error_type=%s",
            safe_tool,
            error_type,
        )
        logger.info(
            "mcp_metric metric=mcp_tool_caller_input_error_total "
            "tool=%s error_type=%s value=1",
            safe_tool,
            error_type,
        )
    except Exception:  # noqa: BLE001 - telemetry must not break the tool call
        pass
