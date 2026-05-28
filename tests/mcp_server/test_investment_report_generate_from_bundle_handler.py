"""ROB-352 Slice A — generate_from_bundle MCP handler contract tests.

Most of these cover the handler's pre-request validation, which short-circuits
BEFORE any DB session is opened, so no DB fixture is needed. The user_id
resolution test monkeypatches the generator so it doesn't touch the bundle
pipeline.
"""

from __future__ import annotations

import uuid

import pytest

from app.mcp_server.tooling import investment_reports_handlers as h
from app.services.action_report.snapshot_backed.generator import (
    _MARKET_ACCOUNT_PAIRS,
)


@pytest.fixture
def _enabled(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )


def _kwargs(**overrides):
    base = {
        "market": "us",
        "account_scope": "kis_live",
        "title": "t",
        "summary": "s",
        "kst_date": "2026-05-29",
        "created_by_profile": "claude_code",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_disabled_returns_structured_error():
    res = await h.investment_report_generate_from_bundle_impl(**_kwargs())
    assert res["success"] is False
    assert res["error"] == "snapshot_backed_report_generator_disabled"


@pytest.mark.asyncio
async def test_unsupported_account_scope_fails_closed(_enabled):
    res = await h.investment_report_generate_from_bundle_impl(
        **_kwargs(account_scope="alpaca_paper")
    )
    assert res["success"] is False
    assert res["error"] == "unsupported_account_scope"
    assert "kis_live" in str(res["supported_pairs"])
    assert "hermes" in res["hint"].lower()


@pytest.mark.asyncio
async def test_unsupported_pair_fails_closed(_enabled):
    # valid literal scope but wrong market pairing
    res = await h.investment_report_generate_from_bundle_impl(
        **_kwargs(market="kr", account_scope="upbit_live")
    )
    assert res["success"] is False
    assert res["error"] == "unsupported_account_scope"


@pytest.mark.asyncio
async def test_invalid_market_session_fails_closed(_enabled):
    res = await h.investment_report_generate_from_bundle_impl(
        **_kwargs(market_session="us_regular")
    )
    assert res["success"] is False
    assert res["error"] == "invalid_market_session"
    assert "pre" in str(res["allowed"])


@pytest.mark.asyncio
async def test_invalid_item_reports_field_and_key(_enabled):
    res = await h.investment_report_generate_from_bundle_impl(
        **_kwargs(
            items=[{"item_kind": "action", "intent": "buy_review", "rationale": "r"}]
        )  # missing required client_item_key
    )
    assert res["success"] is False
    assert res["error"] == "invalid_items"
    assert res["item_errors"][0]["index"] == 0
    assert "client_item_key" in str(res["item_errors"][0]["errors"])


@pytest.mark.asyncio
async def test_invalid_enum_item_names_the_field(_enabled):
    res = await h.investment_report_generate_from_bundle_impl(
        **_kwargs(
            items=[
                {
                    "client_item_key": "k1",
                    "item_kind": "action",
                    "intent": "not_a_real_intent",
                    "rationale": "r",
                }
            ]
        )
    )
    assert res["success"] is False
    assert res["error"] == "invalid_items"
    assert "intent" in str(res["item_errors"][0]["errors"])


@pytest.mark.asyncio
async def test_user_id_defaults_for_live_scope(_enabled, monkeypatch):
    """ROB-352 — omitting user_id resolves the MCP default and surfaces it."""
    from app.services.action_report.snapshot_backed import generator as gen_mod
    from app.services.action_report.snapshot_backed.request import (
        ReportGenerationResponse,
    )

    captured = {}

    async def _fake_generate(self, request):
        captured["request"] = request
        return ReportGenerationResponse(
            report_uuid=uuid.uuid4(),
            snapshot_bundle_uuid=uuid.uuid4(),
            snapshot_policy_version="p",
            snapshot_coverage_summary={},
            snapshot_freshness_summary={},
            source_conflicts={},
            unavailable_sources={},
            items_count=0,
            warnings=[],
            bundle_status="complete",
            bundle_reused=False,
            stale_gate={},
        )

    monkeypatch.setattr(
        gen_mod.SnapshotBackedReportGenerator, "generate", _fake_generate
    )

    res = await h.investment_report_generate_from_bundle_impl(**_kwargs())
    assert res["success"] is True
    expected = h._default_generator_user_id()
    assert res["resolved_user_id"] == expected
    assert captured["request"].user_id == expected


def test_handler_supported_pairs_match_generator():
    """Drift-guard: the handler's allow-list must equal the generator's."""
    assert h._SUPPORTED_MARKET_ACCOUNT_PAIRS == _MARKET_ACCOUNT_PAIRS


def test_tool_description_documents_contract():
    """ROB-352 — the registered tool description must surface the real
    constraints (supported scopes, required item fields, idempotency)."""
    from app.mcp_server.tooling.investment_reports_handlers import (
        GENERATE_FROM_BUNDLE_DESCRIPTION,
    )

    desc = GENERATE_FROM_BUNDLE_DESCRIPTION
    assert "kis_live" in desc
    assert "client_item_key" in desc
    assert "overwrite_existing" in desc
    assert "market_session" in desc


@pytest.mark.asyncio
async def test_overwrite_blocked_returns_structured_error(_enabled, monkeypatch):
    """ROB-352 — ReportOverwriteBlockedError maps to a structured response."""
    from app.services.action_report.snapshot_backed import generator as gen_mod
    from app.services.investment_reports.ingestion import (
        ReportOverwriteBlockedError,
    )

    async def _raise(self, request):
        raise ReportOverwriteBlockedError(
            report_uuid="11111111-1111-1111-1111-111111111111",
            decision_count=2,
            active_alert_count=1,
        )

    monkeypatch.setattr(gen_mod.SnapshotBackedReportGenerator, "generate", _raise)

    res = await h.investment_report_generate_from_bundle_impl(
        **_kwargs(overwrite_existing=True, overwrite_reason="redo")
    )
    assert res["success"] is False
    assert res["error"] == "overwrite_blocked_has_audit"
    assert res["decision_count"] == 2
    assert res["active_alert_count"] == 1
    assert "supersede" in res["hint"].lower() or "revise" in res["hint"].lower()
