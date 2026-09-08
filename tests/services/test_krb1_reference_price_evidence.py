"""AC3 — separated reference-price evidence clocks (effective session vs publication)."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from app.services.krb1_reference_price_evidence import (
    AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES,
    NUMERIC_DEFERRED_METHODS,
    NUMERIC_REQUIRED_METHODS,
    OUTCOME_RULE,
    RUN_FAIL_CLOSE_METHODS,
    ReferencePriceExceptionRecord,
    classify_record,
    evaluate_reference_price_exception_coverage,
    excluded_pending_opening_call_symbols,
    is_tradable_reference_price,
)

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
AS_OF = dt.date(2026, 7, 29)
TARGET = dt.date(2026, 7, 30)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
PUBLISHED_AT = dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST)
RETRIEVED_AT = dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST)
SHA = "a" * 64
SYMBOLS = ("000660", "005930")


def _record(symbol: str, **overrides: object) -> ReferencePriceExceptionRecord:
    base = ReferencePriceExceptionRecord(
        symbol=symbol,
        effective_session=TARGET,
        is_exception=False,
        source="krx_official_base_price",
        published_at=PUBLISHED_AT,
        retrieved_at=RETRIEVED_AT,
        determination_method="NORMAL_PRIOR_CLOSE",
        raw_reference_price="70000",
        raw_reason_code="NORMAL",
        raw_payload_sha256=SHA,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _evaluate(records, **kwargs):
    return evaluate_reference_price_exception_coverage(
        records=records,
        required_symbols=kwargs.get("required_symbols", SYMBOLS),
        target_session=kwargs.get("target_session", TARGET),
        decision_at=kwargs.get("decision_at", DECISION_AT),
        source_unavailable_reason=kwargs.get("source_unavailable_reason"),
    )


def test_allowlist_and_rule_are_explicit() -> None:
    assert AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES == frozenset(
        {"krx_official_base_price"}
    )
    assert OUTCOME_RULE == (
        "effective_session == target_session "
        "AND published_at <= decision_at AND retrieved_at <= decision_at"
    )


def test_pre_decision_publication_for_the_target_session_is_proven() -> None:
    gate = _evaluate([_record(symbol) for symbol in SYMBOLS])

    assert gate.status == "proven"
    assert gate.reason == "target_session_reference_price_exception_coverage_proven"
    assert gate.evidence["checked_count"] == 2


@pytest.mark.parametrize(
    ("overrides", "defect"),
    [
        ({"source": "generic_screener"}, "source_not_authoritative"),
        ({"effective_session": AS_OF}, "effective_session_not_target_session"),
        ({"is_exception": None}, "exception_flag_missing"),
        (
            {"published_at": dt.datetime(2026, 7, 30, 8, 0, tzinfo=KST)},
            "published_after_decision_at",
        ),
        (
            {"retrieved_at": dt.datetime(2026, 7, 30, 8, 30, tzinfo=KST)},
            "retrieved_after_decision_at",
        ),
        (
            {"retrieved_at": dt.datetime(2026, 7, 29, 15, 0, tzinfo=KST)},
            "retrieved_before_published",
        ),
        ({"published_at": dt.datetime(2026, 7, 29, 16, 0)}, "clock_not_timezone_aware"),
        ({"raw_reference_price": None}, "raw_reference_price_missing"),
        ({"raw_reference_price": "0"}, "raw_reference_price_missing"),
        ({"raw_reference_price": "70000.0"}, "raw_reference_price_missing"),
        ({"raw_reason_code": ""}, "raw_reason_code_missing"),
        ({"raw_payload_sha256": None}, "raw_payload_hash_missing"),
        ({"raw_payload_sha256": "nope"}, "raw_payload_hash_missing"),
    ],
)
def test_defects_are_named_and_block(overrides: dict[str, object], defect: str) -> None:
    assert (
        classify_record(
            _record(SYMBOLS[0], **overrides),
            target_session=TARGET,
            decision_at=DECISION_AT,
        )
        == defect
    )

    gate = _evaluate(
        [_record(SYMBOLS[0], **overrides), _record(SYMBOLS[1])],
    )
    assert gate.status == "unprovable"
    assert gate.reason == "target_session_reference_price_exception_unproven"
    assert {"symbol": SYMBOLS[0], "defect": defect} in gate.evidence["defect_examples"]


def test_partial_coverage_blocks() -> None:
    gate = _evaluate([_record(SYMBOLS[0])])
    assert gate.status == "unprovable"
    assert gate.evidence["missing_examples"] == [SYMBOLS[1]]


def test_duplicate_records_block() -> None:
    gate = _evaluate([_record(SYMBOLS[0]), _record(SYMBOLS[0]), _record(SYMBOLS[1])])
    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "duplicate_records"


def test_records_outside_the_required_set_block() -> None:
    gate = _evaluate(
        [_record(SYMBOLS[0]), _record(SYMBOLS[1]), _record("999999")],
    )
    assert gate.status == "unprovable"
    assert gate.evidence["outside_expected_universe_examples"] == ["999999"]


def test_empty_required_set_blocks() -> None:
    gate = _evaluate([], required_symbols=[])
    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "no_symbols_to_prove"


def test_unavailable_source_reason_blocks_first() -> None:
    gate = _evaluate(
        [_record(symbol) for symbol in SYMBOLS],
        source_unavailable_reason="authoritative_source_not_wired",
    )
    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "authoritative_source_unavailable"


def test_naive_decision_clock_blocks() -> None:
    gate = _evaluate(
        [_record(symbol) for symbol in SYMBOLS],
        decision_at=dt.datetime(2026, 7, 29, 18, 0),
    )
    assert gate.status == "unprovable"
    assert gate.reason == (
        "reference_price_exception_decision_clock_not_timezone_aware"
    )


def test_boundary_equality_with_decision_clock_is_accepted() -> None:
    gate = _evaluate(
        [
            _record(symbol, published_at=DECISION_AT, retrieved_at=DECISION_AT)
            for symbol in SYMBOLS
        ]
    )
    assert gate.status == "proven"


def test_exception_flag_controls_tradability_not_the_gate() -> None:
    excepted = _record(SYMBOLS[0], is_exception=True)
    gate = _evaluate([excepted, _record(SYMBOLS[1])])

    assert gate.status == "proven"
    assert gate.evidence["exception_symbols"] == [SYMBOLS[0]]
    assert is_tradable_reference_price(excepted) is False
    assert is_tradable_reference_price(_record(SYMBOLS[1])) is True
    assert is_tradable_reference_price(_record(SYMBOLS[1], is_exception=None)) is False


# ───────────────── A4: numeric price vs determination method ─────────────────


def test_determination_method_catalogue() -> None:
    assert NUMERIC_REQUIRED_METHODS == frozenset(
        {"NORMAL_PRIOR_CLOSE", "PRECOMPUTED_THEORETICAL"}
    )
    assert NUMERIC_DEFERRED_METHODS == frozenset({"TARGET_DAY_OPENING_CALL"})
    assert RUN_FAIL_CLOSE_METHODS == frozenset({"UNKNOWN"})


@pytest.mark.parametrize("method", sorted(NUMERIC_REQUIRED_METHODS))
def test_numeric_price_is_required_for_precomputed_methods(method: str) -> None:
    proven = _record(SYMBOLS[0], determination_method=method)
    assert (
        classify_record(proven, target_session=TARGET, decision_at=DECISION_AT)
        == "proven"
    )
    without = _record(SYMBOLS[0], determination_method=method, raw_reference_price=None)
    assert (
        classify_record(without, target_session=TARGET, decision_at=DECISION_AT)
        == "raw_reference_price_missing"
    )


def test_opening_call_method_is_excluded_not_failed() -> None:
    """🔴 The number does not exist on the prior evening — that is not a proof failure."""
    record = _record(
        SYMBOLS[0],
        determination_method="TARGET_DAY_OPENING_CALL",
        raw_reference_price=None,
    )
    assert (
        classify_record(record, target_session=TARGET, decision_at=DECISION_AT)
        == "excluded_pending_opening_call"
    )
    assert is_tradable_reference_price(record) is False

    gate = _evaluate([record, _record(SYMBOLS[1])])
    assert gate.status == "proven", gate.evidence
    assert gate.evidence["excluded_pending_opening_call"] == [SYMBOLS[0]]
    assert gate.evidence["numeric_price_not_awaited_or_estimated"] is True


def test_opening_call_record_carrying_a_number_is_a_defect() -> None:
    """A number that should not exist yet means the classification is inconsistent."""
    record = _record(
        SYMBOLS[0],
        determination_method="TARGET_DAY_OPENING_CALL",
        raw_reference_price="70000",
    )
    assert (
        classify_record(record, target_session=TARGET, decision_at=DECISION_AT)
        == "numeric_reference_price_before_opening_call_determination"
    )
    assert _evaluate([record, _record(SYMBOLS[1])]).status == "unprovable"


@pytest.mark.parametrize(
    ("method", "defect"),
    [
        ("UNKNOWN", "determination_method_unknown"),
        ("SOMETHING_ELSE", "determination_method_unrecognized"),
    ],
)
def test_unresolved_method_fails_the_run(method: str, defect: str) -> None:
    """🔴 UNKNOWN / unrecognized is run-level fail-close, never an exclusion."""
    record = _record(SYMBOLS[0], determination_method=method)
    assert (
        classify_record(record, target_session=TARGET, decision_at=DECISION_AT)
        == defect
    )
    gate = _evaluate([record, _record(SYMBOLS[1])])
    assert gate.status == "unprovable"
    assert {"symbol": SYMBOLS[0], "defect": defect} in gate.evidence["defect_examples"]


def test_excluded_symbol_helper_lists_only_deferred_methods() -> None:
    records = [
        _record(
            SYMBOLS[0],
            determination_method="TARGET_DAY_OPENING_CALL",
            raw_reference_price=None,
        ),
        _record(SYMBOLS[1]),
    ]
    assert excluded_pending_opening_call_symbols(
        records=records, target_session=TARGET, decision_at=DECISION_AT
    ) == (SYMBOLS[0],)
