"""Name-preserving invocation log for every MCP ``tools/call``.

Why this exists
---------------
The MCP containers ship stdout to Loki through the root formatter configured in
``app.mcp_server.main`` — ``"%(asctime)s [%(levelname)s] %(message)s"``. That
format renders the message only, so any tool name attached through ``extra`` is
dropped before it leaves the process. The pre-existing niche observation
(``app.mcp_server.tooling.niche``) attaches the name exactly that way, which is
why the shipped Loki line is a bare ``[WARNING] mcp.niche_tool_called`` with no
tool in it.

``timeout_middleware`` already demonstrates the shape that survives the formatter
(``"mcp.tool.timeout tool=%s budget_s=%.0f"``): the name goes into the message
string. This middleware follows that precedent instead of changing the formatter,
so no other log line in the container changes shape.

Scope
-----
One line per ``tools/call``, for **every** registered tool rather than only the
audited C ("niche") group, so ``container`` x ``tool`` aggregation becomes
possible. Sentry's ``mcp.niche`` tag is deliberately **not** touched here — it
stays owned by ``tooling.niche`` so that it keeps marking only the audited group.

Only the tool name and the server profile (the container's identity) are logged.
Arguments and results never are: they carry symbols, quantities, prices and
account identifiers.

Field sanitization
------------------
Both logged fields are caller-influenced: ``name`` arrives verbatim over the wire
(unregistered names reach this middleware before dispatch rejects them) and
``profile`` is a public-constructor argument. Before rendering, each field goes
through :func:`sanitize_log_field`, which keeps the line a single physical
record and a clean two-key logfmt pair by escaping, rather than dropping:

- every Unicode category ``C*`` character — C0/C1 controls (NUL, tab, CR, LF,
  ESC/ANSI introducers), format characters (zero-width and bidi marks),
  surrogates, private-use and unassigned code points;
- every Unicode category ``Z*`` character — ASCII space, NBSP and friends, and
  the line/paragraph separators U+2028/U+2029 that ``str.splitlines`` and some
  collectors treat as line breaks;
- ``\\`` (so escapes stay unambiguous), ``%`` (so ``%s``/``%(name)s`` literals
  survive as text if a downstream stage re-formats), ``=`` and ``"`` (so a
  value can never look like a new logfmt key or open a quoted span).

The escaped value stays identifiable — ``x tool=spoofed`` renders as
``x\\x20tool\\x3dspoofed`` — and fields longer than ``MAX_FIELD_CHARS`` source
characters are cut with a ``…(+N)`` suffix so one call can never emit an
unbounded line.

Safety
------
Observation must never change the outcome of a tool call. This repository has a
precedent of an accepted order being turned into a ``RuntimeError`` by a raising
telemetry call (#2049), so the whole observation — attribute read and ``logger``
call alike — sits inside one ``except Exception`` that swallows and continues.
``BaseException`` is intentionally not caught: cancellation and interpreter exit
must still propagate.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.tool import ToolResult

logger = logging.getLogger(__name__)

# Message template. The tool name lives in the message body (not in ``extra``)
# because the container's formatter renders the message only. Keep the
# ``key=value`` shape so Loki's logfmt parser can pick ``tool`` out directly.
TOOL_CALL_MESSAGE = "mcp.tool.called tool=%s profile=%s"

# Used when the request shape or the profile is unexpected; keeps the line
# emittable instead of silently losing the observation.
UNKNOWN_TOOL_NAME = "unknown"
UNKNOWN_PROFILE = "unknown"

# Source characters (pre-escape) kept per logged field. Longer values are cut
# and marked, so a single call can never emit an unbounded line.
MAX_FIELD_CHARS = 128

_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\\": "\\\\"}
# Printable but structurally dangerous: ``%`` would survive as printf-literal
# text, ``=`` and ``"`` could forge a logfmt key or open a quoted value span.
_EXTRA_ESCAPED = frozenset('%="')


def _escape_char(char: str) -> str:
    named = _NAMED_ESCAPES.get(char)
    if named is not None:
        return named
    if unicodedata.category(char)[0] in ("C", "Z") or char in _EXTRA_ESCAPED:
        code = ord(char)
        if code <= 0xFF:
            return f"\\x{code:02x}"
        if code <= 0xFFFF:
            return f"\\u{code:04x}"
        return f"\\U{code:08x}"
    return char


def sanitize_log_field(value: Any) -> str:
    """Render ``value`` as one bounded logfmt-safe token.

    Never raises and never silently empties: an unstringable value becomes the
    fixed ``unreadable`` token, anything else is escaped (see module docstring)
    so the original stays identifiable.
    """
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - a field must not take the line down
        return "unreadable"
    dropped = max(0, len(text) - MAX_FIELD_CHARS)
    rendered = "".join(_escape_char(char) for char in text[:MAX_FIELD_CHARS])
    if dropped:
        rendered += f"…(+{dropped})"
    return rendered


class ToolCallLogMiddleware(Middleware):
    """Emit one name-carrying log line per ``tools/call``.

    Registered FIRST in ``main.py`` so it is the OUTERMOST middleware: the call is
    recorded even when an inner middleware (a timeout budget, say) ends it.
    """

    def __init__(self, *, profile: Any = None) -> None:
        # ``McpProfile`` is a StrEnum, but read ``.value`` explicitly rather than
        # relying on ``str()`` so a plain string or any other carrier renders the
        # same way.
        value = getattr(profile, "value", profile)
        self._profile = UNKNOWN_PROFILE if value is None else sanitize_log_field(value)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        self._observe(context)
        return await call_next(context)

    def _observe(self, context: Any) -> None:
        try:
            tool_name = getattr(getattr(context, "message", None), "name", None)
            logger.info(
                TOOL_CALL_MESSAGE,
                sanitize_log_field(tool_name) if tool_name else UNKNOWN_TOOL_NAME,
                self._profile,
            )
        except Exception:  # noqa: BLE001 - observation never breaks a tool call
            pass
