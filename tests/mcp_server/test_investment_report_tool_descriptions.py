# tests/mcp_server/test_investment_report_tool_descriptions.py
"""ROB-457 — tool descriptions state the valid account_scope set accurately.

Guards against the stale operational note that claimed alpaca_paper is rejected
by the create tools (it is not — only generate_from_bundle restricts to the
live KIS/Upbit pairs).
"""

from __future__ import annotations

import app.mcp_server.tooling.investment_hermes_handlers as hermes_handlers
import app.mcp_server.tooling.investment_reports_handlers as handlers

_VALID_ACCOUNT_SCOPES = ("kis_live", "kis_mock", "alpaca_paper", "upbit_live")


def _capture(register) -> dict[str, str]:
    captured: dict[str, str] = {}

    class _FakeMCP:
        def tool(self, *, name, description):
            captured[name] = description
            return lambda fn: fn

    register(_FakeMCP())
    return captured


def test_create_description_lists_valid_account_scopes():
    desc = _capture(handlers.register_investment_report_tools)[
        "investment_report_create"
    ]
    for scope in _VALID_ACCOUNT_SCOPES:
        assert scope in desc, f"create description must name account_scope {scope!r}"


def test_create_from_hermes_description_advertises_alpaca_paper():
    # generate_from_bundle steers alpaca_paper here; this description should
    # confirm the path accepts it (and all four scopes).
    desc = _capture(hermes_handlers.register_investment_hermes_tools)[
        "investment_report_create_from_hermes_composition"
    ]
    assert "alpaca_paper" in desc


def test_draft_mutation_descriptions_state_draft_only_and_no_broker_mutation():
    captured = _capture(handlers.register_investment_report_tools)

    for name in ("investment_report_add_items", "investment_report_update"):
        desc = captured[name]
        assert "Draft-only" in desc
        assert "No broker / order / watch mutation" in desc


def test_add_items_description_mentions_duplicate_client_item_key():
    desc = _capture(handlers.register_investment_report_tools)[
        "investment_report_add_items"
    ]
    assert "duplicate client_item_key" in desc
