"""hk 6805: classify MCP caller argument errors vs server faults.

MCP callers passing bad arguments used to reach Sentry twice per call:
FastMCP logs ``Error calling tool '<name>'`` with the original exception
(e.g. ``ValueError``) and then re-raises it wrapped in ``ToolError``, which
the sentry-sdk MCP integration captures again at the ``call_tool`` boundary.
Caller input mistakes are not server faults, so they are counted here instead:
one warning log plus one ``mcp_metric`` counter sample per call (Loki-countable,
same convention as ``app.monitoring.execution_metrics``).

The closed caller-input set is ``ValueError`` — which already covers
``pydantic.ValidationError`` because pydantic v2 subclasses it — plus
``fastmcp.exceptions.ValidationError`` raised for argument schema failures.
This mirrors ``_exception_span_status`` in ``app.monitoring.sentry``, which
already maps ``ValueError``/``ValidationError`` to ``INVALID_ARGUMENT``.
``app.monitoring.sentry`` applies the serialized-type equivalent of this set
to drop the matching Sentry events; keep the two lists in sync.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.exceptions import ValidationError as FastMcpValidationError

from app.mcp_server.tool_call_log_middleware import sanitize_log_field

logger = logging.getLogger(__name__)


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

    ``ToolError``/``FastMCPError`` are transparent wrappers FastMCP adds around
    whatever the tool raised, so they are skipped. A single non-wrapper,
    non-caller-input exception anywhere in the chain (a ``RuntimeError``, an
    ``httpx`` error, a DB error) means a server fault and returns False —
    conservative: ambiguous chains stay on the Sentry path.
    """
    if exc is None:
        return False
    saw_input = False
    for link in _exception_chain(exc):
        if isinstance(link, FastMcpValidationError):
            saw_input = True
        elif isinstance(link, ValueError):
            saw_input = True
        elif type(link).__module__.startswith("fastmcp"):
            continue  # ToolError / FastMCPError wrapper
        else:
            return False
    return saw_input


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
