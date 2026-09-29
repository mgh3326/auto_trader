"""Total outcome classification and acknowledgement preservation."""

from __future__ import annotations

import json

import pytest

from app.services.nhplug_mock.outcome import (
    ParsedOrderResponse,
    ResponseMeta,
    classify,
    extract_order_no,
)

pytestmark = pytest.mark.unit
PATH = "/krstock/order/v1/cashBuy"


@pytest.mark.parametrize("http_status", [200, 301, 307, 400, 403, 429, 500, 503])
def test_empty_proof_tables_keep_acknowledgement_uncertain(http_status: int) -> None:
    outcome = classify(
        PATH, ResponseMeta(http_status), ParsedOrderResponse("00000"), "123", None
    )
    assert outcome.state == "uncertain" and outcome.evidence_order_id == "123"


def test_negative_code_without_number_is_not_rejected_until_documented() -> None:
    outcome = classify(
        PATH, ResponseMeta(200), ParsedOrderResponse("40310"), None, None
    )
    assert outcome.state == "uncertain"
    assert (
        classify(
            PATH,
            ResponseMeta(200),
            ParsedOrderResponse("40310"),
            "123",
            None,
            no_order_codes={"40310"},
        ).state
        == "uncertain"
    )


def test_path_scoped_operator_proof_code() -> None:
    assert (
        classify(
            PATH,
            ResponseMeta(200),
            ParsedOrderResponse("00000"),
            "123",
            None,
            success_codes={"00000"},
        ).state
        == "accepted"
    )
    assert (
        classify(
            "/krstock/order/v1/cancel",
            ResponseMeta(200),
            ParsedOrderResponse("00000"),
            "123",
            None,
        ).state
        == "uncertain"
    )


@pytest.mark.parametrize("bad", [None, [], {}, 0, True, object()])
def test_classifier_never_raises_for_malformed_fields(bad: object) -> None:
    outcome = classify(bad, bad, bad, bad, None, success_codes=bad, no_order_codes=bad)
    assert outcome.state == "uncertain"


def test_classifier_internal_error_still_keeps_previously_read_acknowledgement() -> (
    None
):
    class HostileCode:
        def __hash__(self) -> int:
            return hash("00000")

        def __eq__(self, other: object) -> bool:
            raise RuntimeError("injected membership error")

    outcome = classify(
        PATH,
        ResponseMeta(200),
        ParsedOrderResponse("00000"),
        "123",
        None,
        success_codes={HostileCode()},
    )
    assert outcome.state == "uncertain" and outcome.evidence_order_id == "123"


@pytest.mark.parametrize(
    "number", [True, False, 0, -1, "0", "01", "10000000000", "abc", 1.0]
)
def test_unreadable_order_numbers_are_not_evidence(number: object) -> None:
    raw = json.dumps({"Output_0": {"mkt_orr_no": number}}).encode()
    assert extract_order_no(raw) is None
