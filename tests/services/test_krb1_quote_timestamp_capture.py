"""AC5 — GET-only raw quote timestamp capture and the ROB-1121 wrapper witness."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.brokers.kis import constants as kis_constants
from app.services.krb1_evidence_chain import read_records, verify_stream
from app.services.krb1_quote_timestamp_capture import (
    HTTP_METHOD,
    KIS_PRICE_ENDPOINT,
    KIS_PRICE_TR_ID,
    QUOTE_CAPTURE_STREAM_ID,
    QuoteTimestampCapture,
    WrapperFreshnessAnnotation,
    append_quote_capture,
    build_quote_timestamp_capture,
    evaluate_quote_timestamp_capture,
)
from app.services.symbol_analysis.freshness import compute_is_stale

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
SESSION = dt.date(2026, 7, 29)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
CAPTURED_AT = dt.datetime(2026, 7, 29, 15, 40, tzinfo=KST)
SYMBOL = "005930"


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "endpoint": KIS_PRICE_ENDPOINT,
        "tr_id": KIS_PRICE_TR_ID,
        "stck_shrn_iscd": SYMBOL,
        "stck_bsop_date": "20260729",
        "stck_cntg_hour": "153000",
        "stck_prpr": "70000",
    }
    payload.update(overrides)
    return payload


def _capture(**overrides: object) -> QuoteTimestampCapture:
    capture = build_quote_timestamp_capture(
        symbol=SYMBOL,
        raw_payload=_payload(),
        captured_at=CAPTURED_AT,
        raw_payload_sha256="a" * 64,
    )
    return replace(capture, **overrides)  # type: ignore[arg-type]


def _evaluate(capture: QuoteTimestampCapture | None, **kwargs: object):
    return evaluate_quote_timestamp_capture(
        capture=capture,
        symbol=SYMBOL,
        session_date=SESSION,
        decision_at=kwargs.get("decision_at", DECISION_AT),  # type: ignore[arg-type]
        at_or_after=kwargs.get("at_or_after", dt.time(15, 30)),  # type: ignore[arg-type]
    )


def test_endpoint_and_tr_match_the_kis_adapter_constants() -> None:
    assert KIS_PRICE_ENDPOINT == kis_constants.DOMESTIC_PRICE_URL
    assert KIS_PRICE_TR_ID == kis_constants.DOMESTIC_PRICE_TR
    assert HTTP_METHOD == "GET"


def test_raw_broker_timestamp_is_proven() -> None:
    capture = _capture()
    gate = _evaluate(capture)

    assert capture.raw_observed_at() == dt.datetime(2026, 7, 29, 15, 30, tzinfo=KST)
    assert gate.status == "proven"
    assert gate.reason == "selected_quote_actual_raw_timestamp_proven"
    assert gate.evidence["wrapper_fields_are_insufficient"] is True


@pytest.mark.parametrize(
    ("overrides", "defect"),
    [
        ({"raw_business_date": None}, "raw_broker_timestamp_absent_or_malformed"),
        ({"raw_execution_time": None}, "raw_broker_timestamp_absent_or_malformed"),
        ({"raw_execution_time": "996161"}, "raw_broker_timestamp_absent_or_malformed"),
        ({"raw_execution_time": "1530"}, "raw_broker_timestamp_absent_or_malformed"),
        ({"raw_business_date": "20260728"}, "raw_business_date_not_session"),
        ({"raw_execution_time": "090000"}, "raw_execution_time_before_required_bound"),
        ({"raw_execution_time": "190000"}, "raw_execution_time_after_decision_at"),
        ({"raw_symbol": "000660"}, "raw_symbol_mismatch"),
        ({"endpoint": "/uapi/other"}, "endpoint_or_tr_mismatch"),
        ({"tr_id": "FHKST00000000"}, "endpoint_or_tr_mismatch"),
        ({"http_method": "POST"}, "non_get_capture_method"),
        (
            {"captured_at": dt.datetime(2026, 7, 29, 20, 0, tzinfo=KST)},
            "capture_clock_after_decision_at",
        ),
        (
            {"captured_at": dt.datetime(2026, 7, 29, 15, 40)},
            "capture_clock_not_timezone_aware",
        ),
        (
            {"raw_execution_time": "154100"},
            "raw_execution_time_after_capture_clock",
        ),
    ],
)
def test_defects_block_with_named_reasons(
    overrides: dict[str, object], defect: str
) -> None:
    gate = _evaluate(_capture(**overrides))

    assert gate.status == "unprovable"
    assert gate.reason == "selected_quote_actual_raw_timestamp_unproven"
    assert gate.evidence["defect"] == defect


def test_missing_capture_blocks() -> None:
    gate = _evaluate(None)
    assert gate.status == "unprovable"
    assert gate.reason == "selected_quote_raw_timestamp_missing"


def test_naive_decision_clock_blocks() -> None:
    gate = _evaluate(_capture(), decision_at=dt.datetime(2026, 7, 29, 18, 0))
    assert gate.status == "unprovable"
    assert gate.reason == "quote_timestamp_decision_clock_not_timezone_aware"


def test_wrapper_fresh_claim_cannot_substitute_for_raw_fields() -> None:
    capture = _capture(
        raw_business_date=None,
        raw_execution_time=None,
        wrapper=WrapperFreshnessAnnotation(
            price_as_of=CAPTURED_AT.isoformat(),
            price_freshness="fresh",
            is_stale_price=False,
        ),
    )

    gate = _evaluate(capture)
    witness = capture.wrapper_tautology_witness()

    assert gate.status == "unprovable"
    assert witness["captured"] is True
    assert witness["wrapper_freshness_is_evidence"] is False
    assert "wrapper_claims_fresh_without_raw_broker_timestamp" in witness["reasons"]
    assert "wrapper_price_as_of_tracks_local_capture_clock" in witness["reasons"]


def test_witness_flags_wrapper_as_of_that_is_not_the_broker_timestamp() -> None:
    capture = _capture(
        wrapper=WrapperFreshnessAnnotation(
            price_as_of=CAPTURED_AT.isoformat(),
            price_freshness="fresh",
            is_stale_price=False,
        )
    )

    witness = capture.wrapper_tautology_witness()
    assert witness["captured"] is True
    assert "wrapper_price_as_of_is_not_raw_broker_timestamp" in witness["reasons"]
    # The raw fields still prove the timestamp; the wrapper claim is only recorded.
    assert _evaluate(capture).status == "proven"


def test_witness_is_quiet_when_wrapper_agrees_with_the_broker_timestamp() -> None:
    capture = _capture(
        wrapper=WrapperFreshnessAnnotation(
            price_as_of=dt.datetime(2026, 7, 29, 15, 30, tzinfo=KST).isoformat(),
            price_freshness="fresh",
            is_stale_price=False,
        )
    )
    witness = capture.wrapper_tautology_witness()
    assert witness["captured"] is False
    assert witness["reasons"] == []


def test_rob1121_wrapper_freshness_is_tautological_intraday() -> None:
    """The defect the witness documents: same-day as_of can never be stale."""
    intraday_now = dt.datetime(2026, 7, 29, 11, 0, tzinfo=KST)

    assert compute_is_stale("price", intraday_now, trading_date=SESSION) is False
    assert (
        compute_is_stale(
            "price",
            dt.datetime(2026, 7, 29, 9, 0, tzinfo=KST),
            trading_date=SESSION,
        )
        is False
    )


def test_build_rejects_naive_capture_clock() -> None:
    with pytest.raises(ValueError):
        build_quote_timestamp_capture(
            symbol=SYMBOL,
            raw_payload=_payload(),
            captured_at=dt.datetime(2026, 7, 29, 15, 40),
        )


def test_capture_appends_wrapper_annotation_as_non_evidence(tmp_path: Path) -> None:
    path = tmp_path / "quote_timestamp_capture.jsonl"
    capture = _capture(
        wrapper=WrapperFreshnessAnnotation(
            price_as_of=CAPTURED_AT.isoformat(),
            price_freshness="fresh",
            is_stale_price=False,
        )
    )

    appended = append_quote_capture(path, capture)

    assert appended["chain_index"] == 2
    assert appended["stream_id"] == QUOTE_CAPTURE_STREAM_ID
    assert verify_stream(path, stream_id=QUOTE_CAPTURE_STREAM_ID).record_count == 2
    row = read_records(path, stream_id=QUOTE_CAPTURE_STREAM_ID)[1].row
    assert row["wrapper_annotation"]["is_evidence"] is False
    assert row["raw_business_date"] == "20260729"
    assert row["rob1121_wrapper_witness"]["captured"] is True
