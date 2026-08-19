"""MCP server profile definitions.

Profiles gate which tool subsets are registered at startup.
Profile selection is driven by the MCP_PROFILE env var (default: "default").

ROB-1239: for what `route_request`'s `blocked_actions` does and does not mean
relative to this file's registration, see the canonical statement in
`app/mcp_server/tooling/route_request_registration.py`'s `route_request` tool
`description=` string.
"""

from __future__ import annotations

from enum import StrEnum


class McpProfile(StrEnum):
    DEFAULT = "default"
    HERMES_PAPER_KIS = "hermes-paper-kis"
    CRYPTO = "crypto"
    US_PAPER = "us-paper"
    DB_PAPER = "db-paper"
    KIWOOM = "kiwoom"
    # ROB-1159 — least-privilege split of KIWOOM: KR namespace only, the whole
    # kiwoom_mock_us_* namespace (4 mutations + 3 reads) physically absent.
    KIWOOM_KR = "kiwoom_kr"
    SHADOW_REPLAY = "shadow-replay"
    ANALYSIS_READONLY = "analysis_readonly"
    ACCOUNT_READ = "account_read"
    TRADINGCODEX_EXECUTION = "tradingcodex_execution"
    PAPER_EXECUTION = "paper_execution"
    # Canonical physical-account routing surface. The name is a route label;
    # strategy/universe admission is governed by separate contracts.
    ALPACA_PAPER_CLEAN = "alpaca-paper-clean"
    # ROB-1286 — the surface a watch-fire repricing session is spawned with.
    # Closed world: exactly the proposal-only allowlist, so the session can
    # create a proposal and cannot reach any broker order tool.
    WATCH_REPRICING = "watch_repricing"


def resolve_mcp_profile(env: str | None) -> McpProfile:
    """Resolve MCP_PROFILE env value to McpProfile.

    Empty/None → DEFAULT. Invalid string → ValueError.
    """
    normalized = (env or "").strip()
    if not normalized:
        return McpProfile.DEFAULT
    try:
        return McpProfile(normalized)
    except ValueError:
        allowed = ", ".join(f'"{p}"' for p in McpProfile)
        raise ValueError(
            f"Unknown MCP_PROFILE '{normalized}'; allowed values: {allowed}"
        )


__all__ = ["McpProfile", "resolve_mcp_profile"]
