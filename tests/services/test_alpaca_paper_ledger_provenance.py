"""Tests for ApprovalProvenance helpers and _redact_sensitive_keys (ROB-84).

Pure-Python — no DB, no broker imports.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.alpaca_paper_ledger_service import (
    ApprovalProvenance,
    _redact_sensitive_keys,
    from_approval_bridge,
)

# ---------------------------------------------------------------------------
# Provenance: full bridge/briefing/QA inputs
# ---------------------------------------------------------------------------


def _make_bridge(**kwargs):
    from app.schemas.preopen import PreopenPaperApprovalBridge

    defaults = {
        "status": "available",
        "generated_at": datetime(2026, 5, 3, 9, 0, tzinfo=UTC),
        "market_scope": "crypto",
        "stage": "preopen",
        "eligible_count": 1,
        "candidate_count": 1,
        "candidates": [],
        "blocking_reasons": [],
        "warnings": [],
        "unsupported_reasons": [],
    }
    defaults.update(kwargs)
    return PreopenPaperApprovalBridge(**defaults)


def _make_candidate(**kwargs):
    from app.schemas.preopen import PreopenPaperApprovalCandidate

    defaults = {
        "candidate_uuid": uuid4(),
        "symbol": "KRW-BTC",
        "status": "available",
        "signal_symbol": "KRW-BTC",
        "signal_venue": "upbit",
        "execution_symbol": "BTCUSD",
        "execution_venue": "alpaca_paper",
        "execution_asset_class": "crypto",
        "workflow_stage": "crypto_weekend",
        "purpose": "paper_plumbing_smoke",
    }
    defaults.update(kwargs)
    return PreopenPaperApprovalCandidate(**defaults)


def _make_briefing(**kwargs):
    from app.schemas.preopen import PreopenBriefingArtifact, PreopenDecisionSessionCta

    defaults = {
        "status": "ready",
        "run_uuid": uuid4(),
        "market_scope": "crypto",
        "stage": "preopen",
        "cta": PreopenDecisionSessionCta(
            state="create_available",
            label="Create",
            requires_confirmation=True,
        ),
        "qa": {"advisory_only": True},
    }
    defaults.update(kwargs)
    return PreopenBriefingArtifact(**defaults)


@pytest.mark.unit
def test_from_approval_bridge_full_inputs():
    bridge = _make_bridge()
    candidate = _make_candidate()
    briefing = _make_briefing()

    prov = from_approval_bridge(
        bridge,
        candidate,
        briefing_artifact=briefing,
        qa_evaluator_status="ready",
    )

    assert isinstance(prov, ApprovalProvenance)
    assert prov.candidate_uuid == candidate.candidate_uuid
    assert prov.signal_symbol == "KRW-BTC"
    assert prov.signal_venue == "upbit"
    assert prov.execution_asset_class == "crypto"
    assert prov.workflow_stage == "crypto_weekend"
    assert prov.purpose == "paper_plumbing_smoke"
    assert prov.briefing_artifact_run_uuid == briefing.run_uuid
    assert prov.briefing_artifact_status == "ready"
    assert prov.qa_evaluator_status == "ready"
    assert prov.approval_bridge_generated_at == bridge.generated_at
    assert prov.approval_bridge_status == "available"


@pytest.mark.unit
def test_from_approval_bridge_missing_briefing_and_qa():
    bridge = _make_bridge(status="blocked")
    candidate = _make_candidate(
        signal_symbol=None,
        signal_venue=None,
        execution_asset_class=None,
        workflow_stage=None,
        purpose=None,
    )

    prov = from_approval_bridge(bridge, candidate)

    assert prov.briefing_artifact_run_uuid is None
    assert prov.briefing_artifact_status is None
    assert prov.qa_evaluator_status is None
    assert prov.approval_bridge_status == "blocked"
    assert prov.signal_symbol is None
    assert prov.signal_venue is None


@pytest.mark.unit
def test_from_approval_bridge_briefing_no_run_uuid():
    bridge = _make_bridge()
    candidate = _make_candidate()
    briefing = _make_briefing(run_uuid=None, status="degraded")

    prov = from_approval_bridge(bridge, candidate, briefing_artifact=briefing)

    assert prov.briefing_artifact_run_uuid is None
    assert prov.briefing_artifact_status == "degraded"


# ---------------------------------------------------------------------------
# Redaction tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_redact_flat_sensitive_keys():
    sensitive_key_name = "_".join(("api", "key"))
    payload = {
        sensitive_key_name: "synthetic-value",
        "symbol": "BTCUSD",
        "limit_price": "50000",
        "quantity": "0.001",
    }
    redacted = _redact_sensitive_keys(payload)
    assert redacted[sensitive_key_name] == "[REDACTED]"
    assert redacted["symbol"] == "BTCUSD"
    assert redacted["limit_price"] == "50000"
    assert redacted["quantity"] == pytest.approx("0.001")


@pytest.mark.unit
def test_redact_nested_sensitive_keys():
    payload = {
        "order": {
            "symbol": "BTCUSD",
            "secret": "my_secret",
            "authorization": "Bearer abc123",
            "metadata": {
                "account_id": "ACC123456",
                "account_number": "12345678",
                "token": "tok_live_xyz",
                "email": "user@example.com",
            },
        },
        "status": "ok",
    }
    redacted = _redact_sensitive_keys(payload)
    assert redacted["order"]["symbol"] == "BTCUSD"
    assert redacted["order"]["secret"] == "[REDACTED]"
    assert redacted["order"]["authorization"] == "[REDACTED]"
    assert redacted["order"]["metadata"]["account_id"] == "[REDACTED]"
    assert redacted["order"]["metadata"]["account_number"] == "[REDACTED]"
    assert redacted["order"]["metadata"]["token"] == "[REDACTED]"
    assert redacted["order"]["metadata"]["email"] == "[REDACTED]"
    assert redacted["status"] == "ok"


@pytest.mark.unit
def test_redact_list_of_dicts():
    sensitive_key_name = "_".join(("api", "key"))
    payload = [
        {sensitive_key_name: "synthetic-value", "symbol": "BTC"},
        {"token": "***", "qty": "1"},
    ]
    redacted = _redact_sensitive_keys(payload)
    assert isinstance(redacted, list)
    assert redacted[0][sensitive_key_name] == "[REDACTED]"
    assert redacted[0]["symbol"] == "BTC"
    assert redacted[1]["token"] == "[REDACTED]"
    assert redacted[1]["qty"] == "1"


@pytest.mark.unit
def test_redact_preserves_non_sensitive_values():
    payload = {"side": "buy", "order_type": "limit", "qty": 0.001}
    redacted = _redact_sensitive_keys(payload)
    assert redacted == payload


@pytest.mark.unit
def test_redact_empty_dict():
    assert _redact_sensitive_keys({}) == {}


@pytest.mark.unit
def test_redact_non_dict_scalar():
    assert _redact_sensitive_keys("plain string") == "plain string"
    assert _redact_sensitive_keys(42) == 42
    assert _redact_sensitive_keys(None) is None


@pytest.mark.unit
def test_redact_account_no_variant():
    payload = {"account_no": "12345678-01", "symbol": "AAPL"}
    redacted = _redact_sensitive_keys(payload)
    assert redacted["account_no"] == "[REDACTED]"
    assert redacted["symbol"] == "AAPL"
