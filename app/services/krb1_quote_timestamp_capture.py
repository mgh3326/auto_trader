"""AC5 — GET-only capture of the broker's own quote timestamp, plus a ROB-1121 witness.

Two separate facts have to be preserved during a session:

1. **The raw timestamp.** ``price_as_of`` and ``price_freshness`` are wrapper
   fields; ``price_as_of`` can be the local clock and
   :func:`app.services.symbol_analysis.freshness.compute_is_stale` compares
   ``as_of.date() != trading_date``, so intraday it can never report stale
   (ROB-1121). Only KIS' own ``stck_bsop_date`` / ``stck_cntg_hour`` are accepted
   as timestamp evidence here.
2. **The tautology itself.** When the wrapper claims ``fresh`` while the raw
   fields are absent or disagree, that claim is captured as a *witness* — labelled
   non-evidence — so the defect is documented from live data instead of argued.

Capture is GET-only (quotation reads) and append-only. No order, preview, cancel,
DB write, or scheduler surface is reachable from this module, and full completed
session sweeps are one-shot only (see
``scripts/krb1_p0_completed_session_oneshot.py``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.krb1_evidence_chain import append_record, open_stream
from app.services.krb1_gate_result import (
    KST,
    GateResult,
    is_aware,
    kst_datetime,
    normalize_evidence,
    proven,
    to_kst,
    unprovable,
)

SCHEMA_VERSION = "krb1.p0_3.quote_timestamp_capture.v1"
QUOTE_CAPTURE_RECORD_TYPE = "KIS_QUOTE_TIMESTAMP_CAPTURE"
QUOTE_CAPTURE_STREAM_ID = "krb1.p0_3.quote_timestamp_capture"

KIS_PRICE_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-price"
KIS_PRICE_TR_ID = "FHKST01010100"
HTTP_METHOD = "GET"

WRAPPER_FIELDS_ARE_NOT_EVIDENCE = True


@dataclass(frozen=True, slots=True)
class WrapperFreshnessAnnotation:
    """Wrapper-derived freshness claim. Recorded, never trusted."""

    price_as_of: str | None = None
    price_freshness: str | None = None
    is_stale_price: bool | None = None

    def as_canonical(self) -> dict[str, Any]:
        return {
            "is_evidence": False,
            "is_stale_price": self.is_stale_price,
            "price_as_of": self.price_as_of,
            "price_freshness": self.price_freshness,
        }


@dataclass(frozen=True, slots=True)
class QuoteTimestampCapture:
    """One GET-only quote observation with the broker's raw timestamp fields."""

    symbol: str
    endpoint: str
    tr_id: str
    raw_symbol: str | None
    raw_business_date: str | None
    raw_execution_time: str | None
    raw_last_price: str | None
    captured_at: dt.datetime
    http_method: str = HTTP_METHOD
    wrapper: WrapperFreshnessAnnotation | None = None
    raw_payload_sha256: str | None = None

    def raw_observed_at(self) -> dt.datetime | None:
        """KST datetime built strictly from the broker's own raw fields."""
        if not valid_business_date(self.raw_business_date) or not valid_hhmmss(
            self.raw_execution_time
        ):
            return None
        assert self.raw_business_date is not None
        assert self.raw_execution_time is not None
        return dt.datetime(
            int(self.raw_business_date[0:4]),
            int(self.raw_business_date[4:6]),
            int(self.raw_business_date[6:8]),
            int(self.raw_execution_time[0:2]),
            int(self.raw_execution_time[2:4]),
            int(self.raw_execution_time[4:6]),
            tzinfo=KST,
        )

    def wrapper_tautology_witness(self) -> dict[str, Any]:
        """ROB-1121 witness: wrapper claims fresh while raw evidence disagrees."""
        wrapper = self.wrapper
        reasons: list[str] = []
        if wrapper is None:
            return {
                "captured": False,
                "reasons": [],
                "wrapper_freshness_is_evidence": False,
            }
        claims_fresh = wrapper.price_freshness == "fresh" or (
            wrapper.is_stale_price is False
        )
        raw_observed = self.raw_observed_at()
        if claims_fresh and raw_observed is None:
            reasons.append("wrapper_claims_fresh_without_raw_broker_timestamp")
        if claims_fresh and wrapper.price_as_of is None:
            reasons.append("wrapper_claims_fresh_without_price_as_of")
        wrapper_as_of = _parse_iso(wrapper.price_as_of)
        if claims_fresh and wrapper_as_of is not None and raw_observed is not None:
            if wrapper_as_of != raw_observed:
                reasons.append("wrapper_price_as_of_is_not_raw_broker_timestamp")
        if (
            claims_fresh
            and wrapper_as_of is not None
            and is_aware(self.captured_at)
            and to_kst(wrapper_as_of).date() == to_kst(self.captured_at).date()
            and raw_observed is None
        ):
            reasons.append("wrapper_price_as_of_tracks_local_capture_clock")
        return {
            "captured": bool(reasons),
            "reasons": reasons,
            "wrapper_freshness_is_evidence": False,
        }

    def as_canonical(self) -> dict[str, Any]:
        raw_observed = self.raw_observed_at()
        return normalize_evidence(
            {
                "captured_at": self.captured_at,
                "endpoint": self.endpoint,
                "http_method": self.http_method,
                "raw_business_date": self.raw_business_date,
                "raw_execution_time": self.raw_execution_time,
                "raw_last_price": self.raw_last_price,
                "raw_observed_at": raw_observed,
                "raw_payload_sha256": self.raw_payload_sha256,
                "raw_symbol": self.raw_symbol,
                "rob1121_wrapper_witness": self.wrapper_tautology_witness(),
                "symbol": self.symbol,
                "tr_id": self.tr_id,
                "wrapper_annotation": (
                    self.wrapper.as_canonical() if self.wrapper else None
                ),
            }
        )


def valid_hhmmss(value: object) -> bool:
    if type(value) is not str or len(value) != 6 or not value.isdigit():
        return False
    try:
        dt.time(int(value[0:2]), int(value[2:4]), int(value[4:6]))
    except ValueError:
        return False
    return True


def valid_business_date(value: object) -> bool:
    if type(value) is not str or len(value) != 8 or not value.isdigit():
        return False
    try:
        dt.date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return False
    return True


def build_quote_timestamp_capture(
    *,
    symbol: str,
    raw_payload: Mapping[str, Any],
    captured_at: dt.datetime,
    wrapper: WrapperFreshnessAnnotation | None = None,
    raw_payload_sha256: str | None = None,
) -> QuoteTimestampCapture:
    """Project a raw KIS quote payload into a capture record (no fabrication)."""
    if not is_aware(captured_at):
        raise ValueError("captured_at must be timezone-aware")
    return QuoteTimestampCapture(
        symbol=symbol,
        endpoint=str(raw_payload.get("endpoint") or ""),
        tr_id=str(raw_payload.get("tr_id") or ""),
        raw_symbol=_optional_str(raw_payload.get("stck_shrn_iscd")),
        raw_business_date=_optional_str(raw_payload.get("stck_bsop_date")),
        raw_execution_time=_optional_str(raw_payload.get("stck_cntg_hour")),
        raw_last_price=_optional_str(raw_payload.get("stck_prpr")),
        captured_at=captured_at,
        wrapper=wrapper,
        raw_payload_sha256=raw_payload_sha256,
    )


def evaluate_quote_timestamp_capture(
    *,
    capture: QuoteTimestampCapture | None,
    symbol: str,
    session_date: dt.date,
    decision_at: dt.datetime,
    at_or_after: dt.time | None = None,
) -> GateResult:
    """Gate: is the broker's own execution timestamp proven and pre-decision?"""
    required = {
        "required_endpoint": KIS_PRICE_ENDPOINT,
        "required_tr_id": KIS_PRICE_TR_ID,
        "required_raw_fields": ["stck_bsop_date", "stck_cntg_hour"],
        "required_session": session_date.strftime("%Y%m%d"),
        "required_clock_upper_bound_decision_at": normalize_evidence(decision_at),
        "wrapper_fields_are_insufficient": WRAPPER_FIELDS_ARE_NOT_EVIDENCE,
    }
    if at_or_after is not None:
        required["required_time_at_or_after"] = at_or_after.strftime("%H%M%S")
    if not is_aware(decision_at):
        return unprovable(
            "quote_timestamp_decision_clock_not_timezone_aware",
            symbol=symbol,
            **required,
        )
    if capture is None:
        return unprovable(
            "selected_quote_raw_timestamp_missing", symbol=symbol, **required
        )
    evidence = capture.as_canonical()
    if capture.symbol != symbol or capture.raw_symbol != symbol:
        return unprovable(
            "selected_quote_actual_raw_timestamp_unproven",
            defect="raw_symbol_mismatch",
            raw_evidence=evidence,
            **required,
        )
    if capture.endpoint != KIS_PRICE_ENDPOINT or capture.tr_id != KIS_PRICE_TR_ID:
        return unprovable(
            "selected_quote_actual_raw_timestamp_unproven",
            defect="endpoint_or_tr_mismatch",
            raw_evidence=evidence,
            **required,
        )
    if capture.http_method != HTTP_METHOD:
        return unprovable(
            "selected_quote_actual_raw_timestamp_unproven",
            defect="non_get_capture_method",
            raw_evidence=evidence,
            **required,
        )
    raw_observed = capture.raw_observed_at()
    if raw_observed is None:
        return unprovable(
            "selected_quote_actual_raw_timestamp_unproven",
            defect="raw_broker_timestamp_absent_or_malformed",
            raw_evidence=evidence,
            **required,
        )
    if raw_observed.date() != session_date:
        return unprovable(
            "selected_quote_actual_raw_timestamp_unproven",
            defect="raw_business_date_not_session",
            raw_evidence=evidence,
            **required,
        )
    if at_or_after is not None and raw_observed < kst_datetime(
        session_date, at_or_after
    ):
        return unprovable(
            "selected_quote_actual_raw_timestamp_unproven",
            defect="raw_execution_time_before_required_bound",
            raw_evidence=evidence,
            **required,
        )
    if raw_observed > decision_at:
        return unprovable(
            "selected_quote_actual_raw_timestamp_unproven",
            defect="raw_execution_time_after_decision_at",
            raw_evidence=evidence,
            **required,
        )
    if is_aware(capture.captured_at) and capture.captured_at > decision_at:
        return unprovable(
            "selected_quote_actual_raw_timestamp_unproven",
            defect="capture_clock_after_decision_at",
            raw_evidence=evidence,
            **required,
        )
    return proven(
        "selected_quote_actual_raw_timestamp_proven",
        raw_evidence=evidence,
        **required,
    )


def capture_row(capture: QuoteTimestampCapture) -> dict[str, Any]:
    """Canonical append-only row for one capture."""
    row = capture.as_canonical()
    row["recorded_at"] = capture.captured_at.isoformat()
    row["record_type"] = QUOTE_CAPTURE_RECORD_TYPE
    row["schema_version"] = SCHEMA_VERSION
    return row


def append_quote_capture(path: Path, capture: QuoteTimestampCapture) -> dict[str, Any]:
    """Persist one capture append-only; returns the row with chain provenance."""
    open_stream(path, stream_id=QUOTE_CAPTURE_STREAM_ID)
    record = append_record(
        path,
        stream_id=QUOTE_CAPTURE_STREAM_ID,
        record_type=QUOTE_CAPTURE_RECORD_TYPE,
        row=capture_row(capture),
    )
    return {
        "chain_hash": record.chain_hash,
        "chain_index": record.index,
        "row": record.row,
        "stream_id": record.stream_id,
    }


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _parse_iso(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


__all__ = [
    "HTTP_METHOD",
    "KIS_PRICE_ENDPOINT",
    "KIS_PRICE_TR_ID",
    "QUOTE_CAPTURE_RECORD_TYPE",
    "QUOTE_CAPTURE_STREAM_ID",
    "SCHEMA_VERSION",
    "WRAPPER_FIELDS_ARE_NOT_EVIDENCE",
    "QuoteTimestampCapture",
    "WrapperFreshnessAnnotation",
    "append_quote_capture",
    "build_quote_timestamp_capture",
    "capture_row",
    "evaluate_quote_timestamp_capture",
    "valid_business_date",
    "valid_hhmmss",
]
