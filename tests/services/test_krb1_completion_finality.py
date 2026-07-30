"""A3 — local reconcile can never become provider finality.

The whole point of the split: a perfect full-universe exact reconcile must not be
promotable to a provider guarantee. These tests exist to make that promotion
impossible to reintroduce by accident.
"""

from __future__ import annotations

import ast
import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.krb1_completion_finality import (
    FAIL_CLOSED_REASON,
    FINALITY_SOURCE_WIRED,
    FORBIDDEN_CROSS_DOMAIN_SOURCES,
    FinalityFetchResult,
    ProviderFinalityAttestation,
    ProviderFinalityNotWired,
    evaluate_provider_finality,
    evaluate_with_stub,
    fetch_provider_finality,
)

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
SESSION = dt.date(2026, 7, 29)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
DECLARED_AT = dt.datetime(2026, 7, 29, 16, 40, tzinfo=KST)
RETRIEVED_AT = dt.datetime(2026, 7, 29, 16, 50, tzinfo=KST)
MARKET = "KOSPI"
MODULE = Path("app/services/krb1_completion_finality.py")


def _attestation(**overrides: object) -> ProviderFinalityAttestation:
    base = ProviderFinalityAttestation(
        market=MARKET,
        session_date=SESSION,
        source="krx_official_daily_finality",
        revision="1",
        declared_final_at=DECLARED_AT,
        retrieved_at=RETRIEVED_AT,
        correction_policy="corrections republished with an incremented revision",
        raw_payload_sha256="a" * 64,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _evaluate(attestations, **kwargs):
    return evaluate_provider_finality(
        attestations=attestations,
        market=kwargs.get("market", MARKET),
        session_date=kwargs.get("session_date", SESSION),
        decision_at=kwargs.get("decision_at", DECISION_AT),
        local_reconcile_proven=kwargs.get("local_reconcile_proven", True),
        source_unavailable_reason=kwargs.get("source_unavailable_reason"),
    )


# ───────────── the promotion this split exists to prevent ─────────────


def test_a_perfect_local_reconcile_does_not_prove_finality() -> None:
    """🔴 local_reconcile_proven=True and still unprovable."""
    gate = _evaluate([], local_reconcile_proven=True)

    assert gate.status == "unprovable"
    assert gate.reason == "completed_session_provider_finality_unproven"
    assert gate.evidence["defect"] == "no_attestation_for_market_session"
    # The local result is recorded, never used as the verdict.
    assert gate.evidence["local_reconcile_proven"] is True
    assert gate.evidence["local_reconcile_is_not_provider_finality"] is True


def test_the_stub_is_the_only_wired_source_and_it_blocks() -> None:
    assert FINALITY_SOURCE_WIRED is False

    fetched = fetch_provider_finality(
        market=MARKET, session_date=SESSION, decision_at=DECISION_AT
    )
    assert fetched.status == "unprovable"
    assert fetched.reason == FAIL_CLOSED_REASON
    assert fetched.attestations == ()

    gate, again = evaluate_with_stub(
        market=MARKET,
        session_date=SESSION,
        decision_at=DECISION_AT,
        local_reconcile_proven=True,
    )
    assert again.attestations == ()
    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "provider_finality_source_unavailable"
    assert gate.evidence["source_unavailable_reason"] == FAIL_CLOSED_REASON


def test_the_stub_result_refuses_to_carry_an_attestation() -> None:
    for override in (
        {"attestations": (_attestation(),)},
        {"status": "proven"},
        {"reason": "local reconcile was clean"},
        {"source_wired": True},
    ):
        with pytest.raises(ProviderFinalityNotWired):
            FinalityFetchResult(
                market=MARKET,
                session_date=SESSION.isoformat(),
                decision_at=DECISION_AT.isoformat(),
                **override,  # type: ignore[arg-type]
            )


def test_investor_trading_updated_at_cannot_attest_daily_ohlcv() -> None:
    """🔴 Cross-domain substitution is refused at construction.

    Toss investor-trading carries genuine provisional/final semantics, but for the
    investor-trading-amount domain only.
    """
    assert "toss_investor_trading_updated_at" in FORBIDDEN_CROSS_DOMAIN_SOURCES
    for source in sorted(FORBIDDEN_CROSS_DOMAIN_SOURCES):
        with pytest.raises(ProviderFinalityNotWired):
            _attestation(source=source)


def test_module_never_references_the_forbidden_domain_as_a_source() -> None:
    """The forbidden list may be named, but never wired as a fetch path."""
    tree = ast.parse(MODULE.read_text())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "ProviderFinalityAttestation" not in called, (
        "a fail-closed stub must never build an attestation"
    )
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not [module for module in imported if "toss" in module.lower()]


# ───────────── satisfiability, so the block is attributable ─────────────


def test_a_real_attestation_would_prove_it() -> None:
    gate = _evaluate([_attestation()])
    assert gate.status == "proven"
    assert gate.reason == "completed_session_provider_finality_proven"
    assert gate.evidence["attestation"]["revision"] == "1"


@pytest.mark.parametrize(
    ("overrides", "defect"),
    [
        (
            {
                "declared_final_at": dt.datetime(2026, 7, 29, 19, 0, tzinfo=KST),
                "retrieved_at": dt.datetime(2026, 7, 29, 19, 5, tzinfo=KST),
            },
            "declared_final_after_decision_at",
        ),
        (
            {"retrieved_at": dt.datetime(2026, 7, 29, 23, 0, tzinfo=KST)},
            "retrieved_after_decision_at",
        ),
        (
            {"retrieved_at": dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST)},
            "declared_final_after_retrieval_clock",
        ),
    ],
)
def test_attestation_clock_defects_block(
    overrides: dict[str, object], defect: str
) -> None:
    gate = _evaluate([_attestation(**overrides)])
    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == defect


def test_scope_and_conflict_defects_block() -> None:
    assert (
        _evaluate([_attestation(market="KOSDAQ")]).evidence["defect"]
        == "no_attestation_for_market_session"
    )
    assert (
        _evaluate([_attestation(session_date=dt.date(2026, 7, 28))]).evidence["defect"]
        == "no_attestation_for_market_session"
    )
    assert (
        _evaluate([_attestation(), _attestation(revision="2")]).evidence["defect"]
        == "conflicting_attestations"
    )


def test_naive_decision_clock_blocks() -> None:
    gate = _evaluate([_attestation()], decision_at=dt.datetime(2026, 7, 29, 18, 0))
    assert gate.status == "unprovable"
    assert gate.reason == "provider_finality_decision_clock_not_timezone_aware"


def test_attestation_requires_provider_origin_fields() -> None:
    for override in ({"revision": ""}, {"correction_policy": "  "}, {"source": ""}):
        with pytest.raises(ValueError):
            _attestation(**override)
    with pytest.raises(ValueError):
        _attestation(declared_final_at=dt.datetime(2026, 7, 29, 16, 40))
