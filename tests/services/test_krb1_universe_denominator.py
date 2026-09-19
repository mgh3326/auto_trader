"""F-INT-03 — a sealed count cannot be its own denominator.

The gate exists to *block*. These tests pin that: with the shipped constants no
input proves it, and the satisfiable path is only reachable under an explicitly
hypothetical contract that production cannot express.
"""

from __future__ import annotations

import ast
import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from app.services import krb1_universe_denominator as denominator_module
from app.services.krb1_universe_denominator import (
    ADMISSIBLE_EXTERNAL_DENOMINATOR_SOURCES,
    EXTERNAL_DENOMINATOR_SOURCE_WIRED,
    FAIL_CLOSED_REASON,
    PROVEN_REASON,
    REFUSED_SELF_REFERENTIAL_SOURCES,
    UNPROVEN_REASON,
    DenominatorFetchResult,
    ExternalDenominatorNotWired,
    ExternalUniverseDenominator,
    evaluate_universe_denominator,
    evaluate_with_stub,
    fetch_external_universe_denominator,
)

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
SESSION = dt.date(2026, 7, 29)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
PUBLISHED_AT = dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST)
RETRIEVED_AT = dt.datetime(2026, 7, 29, 16, 10, tzinfo=KST)
MARKET = "KOSPI"
HYPOTHETICAL_SOURCE = "krx_official_listed_instrument_count"
MODULE = Path("app/services/krb1_universe_denominator.py")


@pytest.fixture
def wired_denominator_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hypothetical wired official listed-count source, for these tests only.

    🔴 Test-only seam: monkeypatched module attributes, never a parameter. ROB-1175
    owns the real source; until then production cannot reach this state.
    """
    monkeypatch.setattr(denominator_module, "EXTERNAL_DENOMINATOR_SOURCE_WIRED", True)
    monkeypatch.setattr(
        denominator_module,
        "ADMISSIBLE_EXTERNAL_DENOMINATOR_SOURCES",
        frozenset({HYPOTHETICAL_SOURCE}),
    )


def _denominator(**overrides: object) -> ExternalUniverseDenominator:
    base = ExternalUniverseDenominator(
        market=MARKET,
        session_date=SESSION,
        source=HYPOTHETICAL_SOURCE,
        listed_count=942,
        published_at=PUBLISHED_AT,
        retrieved_at=RETRIEVED_AT,
        raw_payload_sha256="a" * 64,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _evaluate(denominators, **kwargs):
    return evaluate_universe_denominator(
        denominators=denominators,
        market=kwargs.get("market", MARKET),
        session_date=kwargs.get("session_date", SESSION),
        decision_at=kwargs.get("decision_at", DECISION_AT),
        sealed_count=kwargs.get("sealed_count", 942),
        actual_count=kwargs.get("actual_count", 942),
        source_unavailable_reason=kwargs.get("source_unavailable_reason"),
    )


# ───────────── the self-proof this gate exists to prevent ─────────────


def test_the_shipped_module_cannot_prove_a_denominator() -> None:
    """🔴 F-INT-03: no external listed count is wired, so nothing proves coverage.

    A first snapshot taken against an already-truncated database seals the short
    count and every one of our numbers agrees with it. Sealing records *when* we
    knew a number, never that it was the whole market.
    """
    assert EXTERNAL_DENOMINATOR_SOURCE_WIRED is False
    assert ADMISSIBLE_EXTERNAL_DENOMINATOR_SOURCES == frozenset()

    gate = _evaluate([_denominator()])

    assert gate.status == "unprovable"
    assert gate.reason == UNPROVEN_REASON
    assert gate.evidence["defect"] == "external_denominator_source_not_wired"
    assert gate.evidence["sealed_count_is_not_external_basis"] is True
    assert gate.evidence["first_snapshot_of_a_truncated_universe_is_self_proving"]


def test_agreement_between_our_own_two_numbers_is_not_evidence(
    wired_denominator_contract: None,
) -> None:
    """sealed == actual is self-consistency. The gate wants an outside number."""
    gate = _evaluate([], sealed_count=1, actual_count=1)

    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "no_external_denominator_for_market_session"
    assert gate.evidence["sealed_count"] == 1
    assert gate.evidence["actual_count"] == 1


def test_an_external_count_that_disagrees_blocks(
    wired_denominator_contract: None,
) -> None:
    gate = _evaluate([_denominator(listed_count=942)], sealed_count=1, actual_count=1)

    assert gate.status == "unprovable"
    assert (
        gate.evidence["defect"] == "universe_denominator_disagrees_with_external_basis"
    )


def test_an_external_count_agreeing_with_a_stale_seal_still_needs_the_rows(
    wired_denominator_contract: None,
) -> None:
    gate = _evaluate([_denominator()], sealed_count=942, actual_count=940)

    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "external_basis_disagrees_with_selected_rows"


def test_a_missing_seal_has_nothing_to_bind(
    wired_denominator_contract: None,
) -> None:
    gate = _evaluate([_denominator()], sealed_count=None)

    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "no_sealed_denominator_to_bind"


# ───────────── refusals at construction ─────────────


def test_our_own_read_cannot_be_relabelled_as_an_external_basis() -> None:
    assert "metadata_snapshot_symbol_count" in REFUSED_SELF_REFERENTIAL_SOURCES
    assert "operator_attestation" in REFUSED_SELF_REFERENTIAL_SOURCES
    for source in sorted(REFUSED_SELF_REFERENTIAL_SOURCES):
        with pytest.raises(ExternalDenominatorNotWired):
            _denominator(source=source)


@pytest.mark.parametrize(
    "overrides",
    [
        {"listed_count": 0},
        {"listed_count": -1},
        {"raw_payload_sha256": "not-a-sha256"},
        {"raw_payload_sha256": ""},
        {"published_at": dt.datetime(2026, 7, 29, 16, 0)},
        {"retrieved_at": dt.datetime(2026, 7, 29, 16, 10)},
        {"source": "   "},
    ],
)
def test_unusable_denominator_values_are_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _denominator(**overrides)


def test_an_unenumerated_source_is_not_admissible(
    wired_denominator_contract: None,
) -> None:
    gate = _evaluate([_denominator(source="some_site_that_lists_stocks")])

    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "external_denominator_source_not_admissible"


def test_a_forged_hash_is_refused_by_the_gate_too(
    wired_denominator_contract: None,
) -> None:
    forged = _denominator()
    object.__setattr__(forged, "raw_payload_sha256", "not-a-sha256")

    gate = _evaluate([forged])

    assert gate.evidence["defect"] == "external_denominator_payload_hash_malformed"


# ───────────── clock bounds ─────────────


@pytest.mark.parametrize(
    ("overrides", "defect"),
    [
        (
            {
                "published_at": dt.datetime(2026, 7, 29, 19, 0, tzinfo=KST),
                "retrieved_at": dt.datetime(2026, 7, 29, 19, 5, tzinfo=KST),
            },
            "external_denominator_published_after_decision_at",
        ),
        (
            {"retrieved_at": dt.datetime(2026, 7, 29, 23, 0, tzinfo=KST)},
            "external_denominator_retrieved_after_decision_at",
        ),
        (
            {"retrieved_at": dt.datetime(2026, 7, 29, 15, 0, tzinfo=KST)},
            "external_denominator_published_after_retrieval_clock",
        ),
    ],
)
def test_late_or_inverted_clocks_block(
    overrides: dict[str, object], defect: str, wired_denominator_contract: None
) -> None:
    gate = _evaluate([_denominator(**overrides)])

    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == defect


def test_naive_decision_clock_blocks() -> None:
    gate = _evaluate([_denominator()], decision_at=dt.datetime(2026, 7, 29, 18, 0))

    assert gate.status == "unprovable"
    assert gate.reason == "universe_denominator_decision_clock_not_timezone_aware"


def test_scope_and_conflict_defects_block(wired_denominator_contract: None) -> None:
    assert (
        _evaluate([_denominator(market="KOSDAQ")]).evidence["defect"]
        == "no_external_denominator_for_market_session"
    )
    assert (
        _evaluate([_denominator(session_date=dt.date(2026, 7, 28))]).evidence["defect"]
        == "no_external_denominator_for_market_session"
    )
    assert (
        _evaluate([_denominator(), _denominator(listed_count=941)]).evidence["defect"]
        == "conflicting_external_denominators"
    )


# ───────────── the stub ─────────────


def test_the_stub_has_no_success_branch() -> None:
    fetched = fetch_external_universe_denominator(
        market=MARKET, session_date=SESSION, decision_at=DECISION_AT
    )

    assert fetched.status == "unprovable"
    assert fetched.reason == FAIL_CLOSED_REASON
    assert fetched.denominators == ()
    assert fetched.source_wired is False
    assert fetched.as_evidence()["sealed_count_is_not_external_basis"] is True


def test_the_stub_result_refuses_to_carry_a_denominator() -> None:
    for override in (
        {"denominators": (_denominator(),)},
        {"status": "proven"},
        {"reason": "the sealed snapshot said so"},
        {"source_wired": True},
    ):
        with pytest.raises(ExternalDenominatorNotWired):
            DenominatorFetchResult(
                market=MARKET,
                session_date=SESSION.isoformat(),
                decision_at=DECISION_AT.isoformat(),
                **override,  # type: ignore[arg-type]
            )


def test_evaluate_with_stub_blocks() -> None:
    gate, fetched = evaluate_with_stub(
        market=MARKET,
        session_date=SESSION,
        decision_at=DECISION_AT,
        sealed_count=1,
        actual_count=1,
    )

    assert fetched.denominators == ()
    assert gate.status == "unprovable"
    assert gate.reason == UNPROVEN_REASON


def test_the_module_never_builds_a_denominator_itself() -> None:
    """A fail-closed stub must not contain a construction site."""
    tree = ast.parse(MODULE.read_text())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "ExternalUniverseDenominator" not in called


# ───────────── satisfiability, so the block is attributable ─────────────


def test_a_real_external_count_would_prove_it(
    wired_denominator_contract: None,
) -> None:
    gate = _evaluate([_denominator()])

    assert gate.status == "proven"
    assert gate.reason == PROVEN_REASON
    assert gate.evidence["denominator"]["listed_count"] == 942
