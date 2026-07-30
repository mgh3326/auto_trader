"""KR-only Kiwoom mock MCP profile registration (ROB-1159).

Least-privilege split of ``MCP_PROFILE=kiwoom``.

``MCP_PROFILE=kiwoom`` registers **both** Kiwoom mock namespaces
unconditionally (``app/mcp_server/tooling/registry.py``, KIWOOM branch): the
eight KR ``kiwoom_mock_*`` tools **and** the seven US ``kiwoom_mock_us_*``
tools, four of which are mutations
(``KIWOOM_MOCK_US_MUTATION_TOOL_NAMES``). Unlike the DEFAULT profile — where
the US namespace is behind ``settings.kiwoom_mock_us_enabled`` (ROB-867) — the
KIWOOM branch has no such gate, so selecting that profile *physically exposes*
the US mutation surface even for a session that only needs KR reads/orders
(for example KR-B1, whose forced profile it is).

``MCP_PROFILE=kiwoom_kr`` is that profile minus the whole US namespace:

- Same shared read-only research/account surface (this profile does **not**
  early-return before the "Always" block), so it is a drop-in replacement for
  ``kiwoom`` in a KR session.
- Exactly the eight KR ``kiwoom_mock_*`` tools as its order surface, including
  the ROB-1155 ``kiwoom_mock_get_order_detail`` (kt00007) read.
- ``orders_kiwoom_us_variants`` is never imported here, and the KR registrar is
  additionally driven through ``_AllowlistedMCP`` so a *future* US (or any
  other unlisted) registration added inside ``orders_kiwoom_variants.register``
  is dropped at registration time rather than silently widening this profile.

🔴 The KR order path is untouched: this module only chooses what is registered.
``dmst_stex_tp=KRX`` pinning, ``MOCK_REJECTED_EXCHANGES``, the
``dry_run=False`` + ``confirm=True`` double gate, and every place/cancel/modify
body live in ``orders_kiwoom_variants`` / ``app/services/brokers/kiwoom/`` and
are reused as-is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from app.mcp_server.tooling.analysis_readonly_registration import _AllowlistedMCP
from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
    LIVE_RECONCILE_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_us_variants import (
    KIWOOM_MOCK_US_MUTATION_TOOL_NAMES,
    KIWOOM_MOCK_US_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_kiwoom_variants import (
    register as register_kiwoom_mock_tools,
)
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.orders_toss_variants import TOSS_LIVE_ORDER_TOOL_NAMES
from app.mcp_server.tooling.paper_limit_order_handler import (
    PAPER_LIMIT_ORDER_TOOL_NAMES,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


# The complete order surface of MCP_PROFILE=kiwoom_kr: the KR namespace only.
KIWOOM_KR_TOOL_NAMES: set[str] = set(KIWOOM_MOCK_TOOL_NAMES)

# Never registered on this profile. The US namespace (both its four mutations
# and its three reads) is the point of the split; the other broker order
# surfaces are excluded exactly as they are on MCP_PROFILE=kiwoom.
KIWOOM_KR_FORBIDDEN_TOOL_NAMES: set[str] = (
    KIWOOM_MOCK_US_TOOL_NAMES
    | ORDER_TOOL_NAMES
    | KIS_LIVE_ORDER_TOOL_NAMES
    | KIS_MOCK_ORDER_TOOL_NAMES
    | LIVE_RECONCILE_TOOL_NAMES
    | TOSS_LIVE_ORDER_TOOL_NAMES
    | PAPER_LIMIT_ORDER_TOOL_NAMES
)

# Re-exported so the regression tests and any reviewer can name the four
# US mutation tools this profile exists to exclude without importing the US
# module (which this profile must never load).
KIWOOM_KR_EXCLUDED_US_MUTATION_TOOL_NAMES: set[str] = set(
    KIWOOM_MOCK_US_MUTATION_TOOL_NAMES
)


def register_kiwoom_kr_tools(mcp: FastMCP) -> None:
    """Register the KR-only Kiwoom mock order surface.

    The allowlist proxy is load-bearing, not decorative: it is what keeps this
    profile KR-only if the KR registrar ever grows a non-KR tool.
    """
    filtered = cast("FastMCP", _AllowlistedMCP(mcp, KIWOOM_KR_TOOL_NAMES))
    register_kiwoom_mock_tools(filtered)


__all__ = [
    "KIWOOM_KR_EXCLUDED_US_MUTATION_TOOL_NAMES",
    "KIWOOM_KR_FORBIDDEN_TOOL_NAMES",
    "KIWOOM_KR_TOOL_NAMES",
    "register_kiwoom_kr_tools",
]
