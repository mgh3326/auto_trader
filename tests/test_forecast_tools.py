# tests/test_forecast_tools.py
"""ROB-650 — MCP tool envelopes + registration wiring for forecast tools."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.forecast_tools import (
    forecast_resolve,
    forecast_save,
    get_forecast_calibration,
    get_forecasts,
)
from app.models.review import TradeForecast

_EXPECTED_NAMES = {
    "forecast_save",
    "forecast_resolve",
    "get_forecasts",
    "get_forecast_calibration",
}


def _terminal_factor_target(
    *,
    actor_principal: str,
    authentication_method: str,
) -> dict:
    return {
        "kind": "terminal_close",
        "direction": "up",
        "target_price": 130.0,
        "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
        "price_adjustment_policy": "explicit-factor-v1",
        "target_to_close_factor": 1.0,
        "adjustment_provenance": {
            "contract_version": "corporate-action-adjustment-v1",
            "authority_type": "licensed_data_vendor",
            "authority_id": "KIS",
            "actor_principal": actor_principal,
            "authentication_method": authentication_method,
            "symbol": "SMCI",
            "action_type": "none",
            "action_ratio": 1.0,
            "effective_date": "2026-06-05",
            "verified_through_date": "2026-06-05",
            "source": "KIS corporate-action feed",
            "source_ref": "artifact://corporate-actions/SMCI/2026-06-05",
            "source_sha256": "a" * 64,
            "source_price_basis": "provider_adjusted",
        },
    }


# --------------------------------------------------------------------------- #
# DB-free wiring tests
# --------------------------------------------------------------------------- #
def test_tool_names_set_complete():
    from app.mcp_server.tooling.forecast_registration import FORECAST_TOOL_NAMES

    assert FORECAST_TOOL_NAMES == _EXPECTED_NAMES


def test_tools_in_available_surface():
    from app.mcp_server import AVAILABLE_TOOL_NAMES

    for name in _EXPECTED_NAMES:
        assert name in AVAILABLE_TOOL_NAMES


def test_register_wires_four_tools():
    from app.mcp_server.tooling.forecast_registration import register_forecast_tools

    registered: list[str] = []

    class _FakeMCP:
        def tool(self, *, name, description):
            registered.append(name)

            def _wrap(fn):
                return fn

            return _wrap

    register_forecast_tools(_FakeMCP())
    assert set(registered) == _EXPECTED_NAMES


def test_registration_describes_terminal_close_contract():
    from app.mcp_server.tooling.forecast_registration import register_forecast_tools

    descriptions: dict[str, str] = {}

    class _FakeMCP:
        def tool(self, *, name, description):
            descriptions[name] = description

            def _wrap(fn):
                return fn

            return _wrap

    register_forecast_tools(_FakeMCP())

    save_description = descriptions["forecast_save"]
    resolve_description = descriptions["forecast_resolve"]
    assert "terminal_close" in save_description
    assert "direction in {up, down}" in save_description
    assert "window-touch-v1-high-gte-low-lte" in save_description
    assert "terminal-close-v1-up-gte-down-lt" in save_description
    assert "explicit-factor-v1" in save_description
    assert "supersession evidence" in save_description
    assert "FORECAST_EVIDENCE_AUTHENTICATED_ACTOR_ID" in save_description
    assert "review-date regular-session close" in resolve_description
    assert "high/low" in resolve_description
    assert "genuinely read-only" in resolve_description
    assert "resolution_fingerprint" in resolve_description


@pytest.mark.asyncio
async def test_save_missing_symbol_envelope():
    # Short-circuits before any DB access.
    res = await forecast_save(
        created_by="claude",
        symbol="   ",
        instrument_type="equity_kr",
        forecast_target={
            "kind": "price_target",
            "direction": "at_or_above",
            "target_price": 1.0,
        },
        probability=0.5,
        review_date="2026-07-15",
    )
    assert res["success"] is False
    assert "symbol" in res["error"]


# ROB-712 — forecast_resolve must expose backfill_missing and forward it to
# resolve_forecast on both the single and the batch path.
@pytest.mark.asyncio
async def test_forecast_resolve_passes_backfill_flag(monkeypatch):
    from app.mcp_server.tooling import forecast_tools

    seen: dict[str, object] = {}

    async def fake_resolve(db, *, forecast_id, persist, backfill_missing=True, **kw):
        seen["backfill_missing"] = backfill_missing
        seen["forecast_id"] = forecast_id
        seen["expected_target_version"] = kw.get("expected_target_version")
        seen["expected_claim_hash"] = kw.get("expected_claim_hash")
        seen["expected_resolution_fingerprint"] = kw.get(
            "expected_resolution_fingerprint"
        )
        return {"status": "unresolved_no_data", "changed": False}

    monkeypatch.setattr(forecast_tools, "resolve_forecast", fake_resolve)

    # Stub the session factory so the test never opens a DB connection.
    # Production shape: _session_factory() -> sessionmaker; () -> session;
    # the session is an async context manager that yields the AsyncSession.
    class _StubSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_a):
            return False

    class _StubSessionMaker:
        def __call__(self) -> _StubSession:
            return _StubSession()

    def _factory():
        return _StubSessionMaker()

    monkeypatch.setattr(forecast_tools, "_session_factory", _factory)

    res = await forecast_resolve(
        forecast_id="x",
        dry_run=True,
        backfill_missing=False,
        expected_target_version=3,
        expected_claim_hash="a" * 64,
        expected_resolution_fingerprint="b" * 64,
    )
    assert res["success"] is True
    assert seen["backfill_missing"] is False
    assert seen["forecast_id"] == "x"
    assert seen["expected_target_version"] == 3
    assert seen["expected_claim_hash"] == "a" * 64
    assert seen["expected_resolution_fingerprint"] == "b" * 64


@pytest.mark.asyncio
async def test_due_batch_forwards_terminal_fail_closed_evidence(monkeypatch):
    from app.mcp_server.tooling import forecast_tools

    due = SimpleNamespace(forecast_id="terminal-id", symbol="SMCI")
    evidence = {
        "target_kind": "terminal_close",
        "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
        "review_date": "2026-08-20",
    }

    async def fake_due(_db, *, limit):
        assert limit == 25
        return [due]

    async def fake_quarantined(_db, *, limit):
        assert limit == 25
        return []

    async def fake_resolve(_db, **_kwargs):
        return {
            "status": "requires_adjustment_evidence",
            "changed": False,
            "reason": "factor required",
            "resolution_evidence": evidence,
        }

    class _StubSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_a):
            return False

    class _StubSessionMaker:
        def __call__(self) -> _StubSession:
            return _StubSession()

    monkeypatch.setattr(forecast_tools, "list_due_forecasts", fake_due)
    monkeypatch.setattr(
        forecast_tools, "list_due_quarantined_forecasts", fake_quarantined
    )
    monkeypatch.setattr(forecast_tools, "resolve_forecast", fake_resolve)
    monkeypatch.setattr(forecast_tools, "_session_factory", lambda: _StubSessionMaker())

    result = await forecast_resolve(dry_run=True, backfill_missing=False)

    assert result["success"] is True
    assert result["results"][0]["resolution_evidence"] == evidence


# --------------------------------------------------------------------------- #
# DB-backed envelope tests (opt-in: request the _clean fixture explicitly so
# the wiring tests above stay DB-free)
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def _clean(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
) -> AsyncSession:
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()
    return db_session


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_and_resolve_envelope(_clean):
    saved = await forecast_save(
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.7,
        review_date="2026-07-15",
        session_label="s1",
    )
    assert saved["success"] is True
    assert saved["action"] == "created"
    fid = saved["data"]["forecast_id"]

    preview = await forecast_resolve(
        forecast_id=fid,
        dry_run=True,
        manual_outcome=True,
        manual_evidence=["target hit"],
    )
    assert preview["success"] is True
    assert preview["dry_run"] is True
    assert preview["status"] == "previewed"
    assert preview["changed"] is False

    committed = await forecast_resolve(
        forecast_id=fid,
        dry_run=False,
        manual_outcome=True,
        manual_evidence=["target hit"],
    )
    assert committed["success"] is True
    assert committed["status"] == "resolved"
    assert committed["changed"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_due_batch_dry_run_distinguishes_no_claim_auto_close(_clean):
    saved = await forecast_save(
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "no_resolvable_forecast"},
        probability=0.0,
        review_date="2020-01-01",
    )
    fid = saved["data"]["forecast_id"]

    preview = await forecast_resolve(dry_run=True, backfill_missing=False)

    assert preview["success"] is True
    assert preview["dry_run"] is True
    assert preview["by_status"] == {"would_close_no_claim": 1}
    assert preview["results"] == [
        {
            "forecast_id": fid,
            "symbol": "005930",
            "status": "would_close_no_claim",
            "changed": False,
            "auto_close": True,
            "computed": None,
            "reason": "placeholder has no resolvable claim",
        }
    ]

    committed = await forecast_resolve(dry_run=False, backfill_missing=False)
    assert committed["by_status"] == {"closed_no_claim": 1}
    assert committed["results"][0]["auto_close"] is True

    due_after_close = await forecast_resolve(dry_run=True, backfill_missing=False)
    assert due_after_close["due_count"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_validation_error_envelope(_clean):
    res = await forecast_save(
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={
            "kind": "price_target",
            "direction": "at_or_above",
            "target_price": 1.0,
        },
        probability=1.9,
        review_date="2026-07-15",
    )
    assert res["success"] is False
    assert "probability" in res["error"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_terminal_close_validation_error_envelope(_clean):
    res = await forecast_save(
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target={
            "kind": "terminal_close",
            "direction": "at_or_below",
            "target_price": 30.56,
            "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
            "price_adjustment_policy": "unverified_fail_closed",
        },
        probability=0.52,
        review_date="2026-08-20",
    )

    assert res["success"] is False
    assert "terminal_close.direction" in res["error"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explicit_factor_tool_rejects_payload_actor_without_bearer_binding(
    _clean,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings,
        "FORECAST_EVIDENCE_AUTHENTICATED_ACTOR_ID",
        "service:forecast-mcp",
    )
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)

    res = await forecast_save(
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_factor_target(
            actor_principal="service:forecast-mcp",
            authentication_method="mcp_bearer",
        ),
        probability=0.48,
        review_date="2026-06-05",
    )

    assert res["success"] is False
    assert "authenticated forecast evidence actor" in res["error"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explicit_factor_tool_binds_actor_to_active_mcp_bearer(
    _clean,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings,
        "FORECAST_EVIDENCE_AUTHENTICATED_ACTOR_ID",
        "service:forecast-mcp",
    )
    monkeypatch.setenv("MCP_AUTH_TOKEN", "test-only-token")

    res = await forecast_save(
        created_by="claude",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target=_terminal_factor_target(
            actor_principal="service:forecast-mcp",
            authentication_method="mcp_bearer",
        ),
        probability=0.48,
        review_date="2026-06-05",
    )

    assert res["success"] is True
    binding = res["data"]["semantics_evidence"]["adjustment_authentication"]
    assert binding["contract_version"] == "forecast-evidence-authentication-v1"
    assert binding["actor_principal"] == "service:forecast-mcp"
    assert binding["authentication_method"] == "mcp_bearer"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_due_batch_reports_legacy_quarantine_without_consuming_due_limit(_clean):
    legacy = TradeForecast(
        created_by="legacy-writer",
        symbol="SMCI",
        instrument_type="equity_us",
        forecast_target={
            "kind": "price_target",
            "direction": "at_or_above",
            "target_price": 130.0,
        },
        probability=0.6,
        review_date=date(2020, 1, 1),
        status="open",
    )
    _clean.add(legacy)
    await _clean.commit()
    await _clean.refresh(legacy)

    result = await forecast_resolve(
        dry_run=True,
        backfill_missing=False,
        limit=1,
    )

    assert result["success"] is True
    assert result["due_count"] == 0
    assert result["quarantined_count"] == 1
    assert result["results"][0]["forecast_id"] == str(legacy.forecast_id)
    assert result["results"][0]["status"] == "quarantined_legacy_price_target"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_and_calibration_envelope(_clean):
    lst = await get_forecasts(status="open")
    assert lst["success"] is True
    assert "entries" in lst

    agg = await get_forecast_calibration(group_by="created_by")
    assert agg["success"] is True
    assert agg["group_by"] == "created_by"
    assert "groups" in agg
