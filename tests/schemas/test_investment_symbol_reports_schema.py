"""ROB-301 T2 — Hermes symbol-reduction ingest schema tests.

Covers: happy parse, D11 verdict-field rejection (extra=forbid), vocab guards
(decision_bucket/side) reusing the model's single-source tuples (D5), the
data_available/decision_bucket invariant, empty/duplicate/oversized batch guards.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.models.investment_symbol_intermediate_reports import DECISION_BUCKETS
from app.schemas.investment_symbol_reports import (
    MAX_SYMBOL_REPORTS_PER_CALL,
    HermesSymbolReductionResult,
    HermesSymbolReportsIngestRequest,
)


def _envelope() -> dict:
    return {
        "run_uuid": str(uuid.uuid4()),
        "snapshot_bundle_uuid": str(uuid.uuid4()),
        "market": "kr",
        "account_scope": "kis_live",
    }


def _result(**overrides) -> dict:
    base = {
        "symbol": "005930.KS",
        "symbol_name": "Samsung Electronics",
        "decision_bucket": "new_buy_candidate",
        "side": "buy",
        "confidence": 70,
        "rationale": "fresh quote + candidate evidence",
        "buy_evidence": [{"snapshot_uuid": str(uuid.uuid4())}],
    }
    base.update(overrides)
    return base


def _request(reports: list[dict]) -> dict:
    return {"run_envelope": _envelope(), "symbol_reports": reports}


def test_valid_payload_parses():
    req = HermesSymbolReportsIngestRequest.model_validate(_request([_result()]))
    assert req.request_version == "hermes-symbol-reports.v1"
    assert req.report_kind == "final_report_symbol"
    assert req.symbol_reports[0].decision_bucket == "new_buy_candidate"


def test_verdict_field_is_rejected_d11():
    # Hermes must NOT supply verdict — the service derives it. extra=forbid.
    with pytest.raises(ValidationError):
        HermesSymbolReductionResult.model_validate(_result(verdict="buy"))


@pytest.mark.parametrize(
    "forbidden", ["content_hash", "idempotency_key", "artifact_version"]
)
def test_service_owned_fields_rejected(forbidden):
    with pytest.raises(ValidationError):
        HermesSymbolReductionResult.model_validate(_result(**{forbidden: "x"}))


def test_decision_bucket_out_of_vocab_rejected():
    with pytest.raises(ValidationError):
        HermesSymbolReductionResult.model_validate(_result(decision_bucket="made_up"))


def test_side_out_of_vocab_rejected():
    with pytest.raises(ValidationError):
        HermesSymbolReductionResult.model_validate(_result(side="sideways"))


def test_all_canonical_buckets_accepted_d5():
    for bucket in DECISION_BUCKETS:
        # deferred_no_action is the only legal bucket for an unavailable symbol,
        # but with data_available=True any bucket value parses at the schema layer.
        r = HermesSymbolReductionResult.model_validate(_result(decision_bucket=bucket))
        assert r.decision_bucket == bucket


def test_data_available_requires_bucket():
    with pytest.raises(ValidationError):
        HermesSymbolReductionResult.model_validate(
            _result(data_available=True, decision_bucket=None)
        )


def test_unavailable_symbol_omits_bucket_ok():
    r = HermesSymbolReductionResult.model_validate(
        {"symbol": "AAA.KS", "data_available": False}
    )
    assert r.data_available is False
    assert r.decision_bucket is None


def test_empty_batch_rejected():
    with pytest.raises(ValidationError):
        HermesSymbolReportsIngestRequest.model_validate(_request([]))


def test_duplicate_symbols_rejected():
    with pytest.raises(ValidationError):
        HermesSymbolReportsIngestRequest.model_validate(
            _request([_result(), _result()])  # same symbol twice
        )


def test_oversized_batch_rejected():
    reports = [
        _result(symbol=f"S{i}.KS") for i in range(MAX_SYMBOL_REPORTS_PER_CALL + 1)
    ]
    with pytest.raises(ValidationError):
        HermesSymbolReportsIngestRequest.model_validate(_request(reports))
