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

from app.services import krb1_completion_finality as finality_module
from app.services.krb1_completion_finality import (
    ADMISSIBLE_FINALITY_SOURCES,
    FAIL_CLOSED_REASON,
    FINALITY_SOURCE_WIRED,
    FORBIDDEN_CROSS_DOMAIN_SOURCES,
    REFUSED_SELF_ASSERTED_SOURCES,
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
HYPOTHETICAL_SOURCE = "krx_official_daily_finality"


@pytest.fixture
def wired_finality_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant a hypothetical wired finality source, for these tests only.

    🔴 Test-only seam, deliberately a monkeypatch of module attributes rather than
    a parameter: production has no way to reach it (ROB-1172 F-INT-01/F-INT-04).
    Without it every assertion about a *later* check would be masked by the
    unwired-source refusal, and a gate that can never pass cannot be shown to
    block for the right reason.
    """
    monkeypatch.setattr(finality_module, "FINALITY_SOURCE_WIRED", True)
    monkeypatch.setattr(
        finality_module, "ADMISSIBLE_FINALITY_SOURCES", frozenset({HYPOTHETICAL_SOURCE})
    )


def _attestation(**overrides: object) -> ProviderFinalityAttestation:
    base = ProviderFinalityAttestation(
        market=MARKET,
        session_date=SESSION,
        source=HYPOTHETICAL_SOURCE,
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


def test_a_perfect_local_reconcile_does_not_prove_finality(
    wired_finality_contract: None,
) -> None:
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
    # The unwired refusal is checked first, so it is the attributed defect here.
    # The stub's own reason is what blocks once a source is hypothetically wired;
    # see test_a_wired_but_unavailable_source_still_blocks.
    assert gate.evidence["defect"] == "provider_finality_source_not_wired"
    assert gate.evidence["provider_finality_source_wired"] is False


def test_a_wired_but_unavailable_source_still_blocks(
    wired_finality_contract: None,
) -> None:
    gate, fetched = evaluate_with_stub(
        market=MARKET,
        session_date=SESSION,
        decision_at=DECISION_AT,
        local_reconcile_proven=True,
    )
    assert fetched.attestations == ()
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


def test_a_real_attestation_would_prove_it(wired_finality_contract: None) -> None:
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
    overrides: dict[str, object], defect: str, wired_finality_contract: None
) -> None:
    gate = _evaluate([_attestation(**overrides)])
    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == defect


def test_scope_and_conflict_defects_block(wired_finality_contract: None) -> None:
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


# ───── F-INT-01: the three elements of the verifier's bypass, each anchored ─────


def test_unwired_source_means_no_input_can_prove_finality() -> None:
    """🔴 Element 1 of the verifier's repro: ``FINALITY_SOURCE_WIRED=False``.

    A stub that only stops the *fetch* path is decoration. If a hand-built
    attestation handed in at the value boundary can still reach ``proven``, the
    selector's evidence asserts a provider guarantee that no provider gave — which
    is the whole defect the A3 split exists to remove.
    """
    assert FINALITY_SOURCE_WIRED is False
    assert ADMISSIBLE_FINALITY_SOURCES == frozenset()

    gate = _evaluate([_attestation()])

    assert gate.status == "unprovable"
    assert gate.reason == "completed_session_provider_finality_unproven"
    assert gate.evidence["defect"] == "provider_finality_source_not_wired"
    assert gate.evidence["provider_finality_source_wired"] is False
    assert gate.evidence["attestation_count"] == 1


def test_operator_attestation_cannot_be_built_or_admitted() -> None:
    """🔴 Element 2: the rejected fallback.

    ROB-1172 E1/D1: "operator attestation is not adopted — an operator signature
    cannot create the external fact of a provider publication/effective clock."
    The verifier reached ``proven`` with ``source='operator_attestation'``, so the
    source is refused at construction *and* at the gate.
    """
    assert "operator_attestation" in REFUSED_SELF_ASSERTED_SOURCES
    for source in sorted(REFUSED_SELF_ASSERTED_SOURCES):
        with pytest.raises(ProviderFinalityNotWired):
            _attestation(source=source)


def test_operator_attestation_is_refused_by_the_gate_not_only_the_constructor(
    wired_finality_contract: None,
) -> None:
    """Construction is one layer; the verdict must refuse it independently."""
    forged = _attestation()
    object.__setattr__(forged, "source", "operator_attestation")

    gate = _evaluate([forged])

    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "finality_source_self_asserted_or_cross_domain"
    assert gate.evidence["operator_attestation_is_not_provider_evidence"] is True


def test_malformed_payload_hash_is_refused_at_construction_and_at_the_gate(
    wired_finality_contract: None,
) -> None:
    """🔴 Element 3: ``raw_payload_sha256='not-a-sha256'`` passed unexamined.

    A digest that is not a digest binds the attestation to no payload at all.
    """
    for bad in ("not-a-sha256", "", "A" * 63, "z" * 64, "a" * 65):
        with pytest.raises(ValueError):
            _attestation(raw_payload_sha256=bad)

    forged = _attestation()
    object.__setattr__(forged, "raw_payload_sha256", "not-a-sha256")
    gate = _evaluate([forged])

    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "raw_payload_sha256_malformed"


def test_source_admissibility_is_an_allowlist_so_unenumerated_names_cannot_leak(
    wired_finality_contract: None,
) -> None:
    """Enumerating bad sources leaks; enumerating good ones does not.

    The named refusals above keep the rejected decisions attributable, but the
    load-bearing closure is that anything not admitted is refused.
    """
    gate = _evaluate([_attestation(source="some_source_nobody_reviewed")])

    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "finality_source_not_admissible"
    assert gate.evidence["required_admissible_sources"] == [HYPOTHETICAL_SOURCE]


def test_the_shipped_module_admits_nothing() -> None:
    """Production state, stated once so a widening diff has to change this line."""
    assert ADMISSIBLE_FINALITY_SOURCES == frozenset()
    assert FINALITY_SOURCE_WIRED is False
    assert "operator_attestation" in REFUSED_SELF_ASSERTED_SOURCES
