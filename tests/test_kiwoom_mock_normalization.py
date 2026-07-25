"""ROB-824 stable Kiwoom mock read envelopes and evidence safety."""

from __future__ import annotations

import pytest

from app.services.brokers.kiwoom.normalization import (
    REDACTED_VALUE,
    KiwoomMockEvidenceError,
    build_mock_provenance,
    normalize_deposit,
    normalize_orderable_cash,
    normalize_orders,
    normalize_positions,
    redact_broker_response,
    validate_mock_response_provenance,
)


def test_normalize_kt00018_positions_uses_official_fields() -> None:
    payload = {
        "return_code": 0,
        "acnt_evlt_remn_indv_tot": [
            {
                "stk_cd": "A005930",
                "rmnd_qty": "+000000000000003",
                "pur_pric": "+000000000072,300",
            }
        ],
    }

    assert normalize_positions(payload) == [
        {
            "symbol": "005930",
            "quantity": 3,
            "average_price": 72_300,
            "currency": "KRW",
        }
    ]


def test_normalize_kt00018_empty_positions_is_stable_empty_list() -> None:
    assert normalize_positions({"return_code": 0, "acnt_evlt_remn_indv_tot": []}) == []


# ---------------------------------------------------------------------------
# ROB-891 — kt00001 deposit (ord_alow_amt) and kt00010 orderable (ord_alowa)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("50000000", 50_000_000),
        ("0", 0),
        ("+000000050000000", 50_000_000),
        ("50,000,000", 50_000_000),
        ("+000,000,050,000,000", 50_000_000),
    ],
)
def test_normalize_deposit_parses_ord_alow_amt(raw: str, expected: int) -> None:
    assert normalize_deposit({"return_code": 0, "ord_alow_amt": raw}) == expected


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"return_code": 0}, "missing required cash field"),
        ({"return_code": 0, "ord_alow_amt": None}, "missing required cash field"),
        ({"return_code": 0, "ord_alow_amt": ""}, "missing required cash field"),
        ({"return_code": 0, "ord_alow_amt": "not-a-number"}, "not an integer"),
        ({"return_code": 0, "ord_alow_amt": "-1"}, "negative"),
    ],
)
def test_normalize_deposit_rejects_invalid_evidence(payload: dict, match: str) -> None:
    with pytest.raises(KiwoomMockEvidenceError, match=match):
        normalize_deposit(payload)


def test_normalize_deposit_rejects_prsm_dpst_aset_amt_only() -> None:
    payload = {"return_code": 0, "prsm_dpst_aset_amt": "999999999"}
    with pytest.raises(KiwoomMockEvidenceError, match="missing required cash field"):
        normalize_deposit(payload)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1500000", 1_500_000),
        ("0", 0),
        ("+000000001500000", 1_500_000),
        ("1,500,000", 1_500_000),
    ],
)
def test_normalize_orderable_cash_parses_ord_alowa(raw: str, expected: int) -> None:
    assert normalize_orderable_cash({"return_code": 0, "ord_alowa": raw}) == expected


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"return_code": 0}, "missing required cash field"),
        ({"return_code": 0, "ord_alowa": None}, "missing required cash field"),
        ({"return_code": 0, "ord_alowa": ""}, "missing required cash field"),
        ({"return_code": 0, "ord_alowa": "abc"}, "not an integer"),
        ({"return_code": 0, "ord_alowa": "-5"}, "negative"),
    ],
)
def test_normalize_orderable_cash_rejects_invalid_evidence(
    payload: dict, match: str
) -> None:
    with pytest.raises(KiwoomMockEvidenceError, match=match):
        normalize_orderable_cash(payload)


def test_normalize_orderable_cash_rejects_cross_endpoint_fields() -> None:
    payload = {
        "return_code": 0,
        "ord_psbl_cash": "1500000",
        "entr": "987654",
    }
    with pytest.raises(KiwoomMockEvidenceError, match="missing required cash field"):
        normalize_orderable_cash(payload)


def test_normalize_kt00009_orders_derives_stable_status_and_quantities() -> None:
    payload = {
        "return_code": 0,
        "acnt_ord_cntr_prst_array": [
            {
                "ord_no": "0000001",
                "stk_cd": "A005930",
                "ord_qty": "10",
                "ord_uv": "72300",
                "cntr_qty": "0",
                "cntr_uv": "0",
                "mdfy_cncl_tp": "",
            },
            {
                "ord_no": "0000002",
                "stk_cd": "A000660",
                "ord_qty": "10",
                "ord_uv": "210000",
                "cntr_qty": "4",
                "cntr_uv": "209500",
                "mdfy_cncl_tp": "",
            },
            {
                "ord_no": "0000003",
                "stk_cd": "A035420",
                "ord_qty": "2",
                "ord_uv": "180000",
                "cntr_qty": "2",
                "cntr_uv": "179500",
                "mdfy_cncl_tp": "",
            },
            {
                "ord_no": "0000004",
                "stk_cd": "A051910",
                "ord_qty": "3",
                "ord_uv": "400000",
                "cntr_qty": "0",
                "cntr_uv": "0",
                "mdfy_cncl_tp": "취소",
            },
        ],
    }

    assert normalize_orders(payload) == [
        {
            "order_id": "0000001",
            "symbol": "005930",
            "status": "open",
            "ordered_price": 72_300,
            "filled_quantity": 0,
            "average_price": 0,
            "remaining_quantity": 10,
        },
        {
            "order_id": "0000002",
            "symbol": "000660",
            "status": "partially_filled",
            "ordered_price": 210_000,
            "filled_quantity": 4,
            "average_price": 209_500,
            "remaining_quantity": 6,
        },
        {
            "order_id": "0000003",
            "symbol": "035420",
            "status": "filled",
            "ordered_price": 180_000,
            "filled_quantity": 2,
            "average_price": 179_500,
            "remaining_quantity": 0,
        },
        {
            "order_id": "0000004",
            "symbol": "051910",
            "status": "cancelled",
            "ordered_price": 400_000,
            "filled_quantity": 0,
            "average_price": 0,
            "remaining_quantity": 0,
        },
    ]


@pytest.mark.parametrize(
    ("normalizer", "payload"),
    [
        (
            normalize_positions,
            {
                "return_code": 0,
                "acnt_evlt_remn_indv_tot": [
                    {"stk_cd": "A005930", "rmnd_qty": "not-a-number"}
                ],
            },
        ),
        (
            normalize_orders,
            {
                "return_code": 0,
                "acnt_ord_cntr_prst_array": [
                    {
                        "ord_no": "0000001",
                        "stk_cd": "A005930",
                        "ord_qty": "10",
                    }
                ],
            },
        ),
    ],
)
def test_normalizers_fail_closed_on_malformed_required_fields(
    normalizer, payload
) -> None:
    with pytest.raises(KiwoomMockEvidenceError):
        normalizer(payload)


@pytest.mark.parametrize("symbol", ["Ａ００５９３０", "A００５９３０", "٠٠٥٩٣٠"])
def test_normalize_positions_rejects_unicode_digit_symbols(symbol: str) -> None:
    payload = {
        "return_code": 0,
        "acnt_evlt_remn_indv_tot": [
            {"stk_cd": symbol, "rmnd_qty": "1", "pur_pric": "70000"}
        ],
    }

    with pytest.raises(KiwoomMockEvidenceError, match="KRX symbol"):
        normalize_positions(payload)


def test_redact_broker_response_deep_copies_and_redacts_sensitive_fields() -> None:
    payload = {
        "return_code": 0,
        "authorization": "Bearer secret-token",
        "nested": {
            "app_key": "secret-app-key",
            "app_secret": "secret-app-secret",
            "account_no": "secret-account",
            "evidence": "preserved",
        },
        "rows": [{"token": "secret-token", "ord_no": "0000001"}],
    }

    redacted = redact_broker_response(payload)

    assert redacted == {
        "return_code": 0,
        "authorization": REDACTED_VALUE,
        "nested": {
            "app_key": REDACTED_VALUE,
            "app_secret": REDACTED_VALUE,
            "account_no": REDACTED_VALUE,
            "evidence": "preserved",
        },
        "rows": [{"token": REDACTED_VALUE, "ord_no": "0000001"}],
    }
    assert payload["authorization"] == "Bearer secret-token"


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "x-api-key",
        "x-app-key",
        "accountno",
        "ACNTNO",
        "acctno",
        "acctid",
    ],
)
def test_redact_broker_response_covers_header_and_compact_account_aliases(
    sensitive_key: str,
) -> None:
    payload = {"nested": {sensitive_key: "must-not-escape"}}

    redacted = redact_broker_response(payload)

    assert redacted["nested"][sensitive_key] == REDACTED_VALUE
    assert payload["nested"][sensitive_key] == "must-not-escape"


@pytest.mark.parametrize(
    "conflicting_provenance",
    [
        {"environment": "live"},
        {"account_mode": "kiwoom_live"},
        {"source": "kiwoom_live"},
        {"is_mock": False},
        {"host": "api.kiwoom.com"},
        {"base_url": "https://api.kiwoom.com"},
    ],
)
def test_live_provenance_conflict_fails_closed(conflicting_provenance) -> None:
    with pytest.raises(KiwoomMockEvidenceError, match="provenance"):
        validate_mock_response_provenance({"provenance": conflicting_provenance})


@pytest.mark.parametrize(
    "conflicting_provenance",
    [
        {"accountMode": "kiwoom_live"},
        {"account-mode": "kiwoom_live"},
        {"isMock": False},
        {"is-mock": False},
        {"baseUrl": "https://api.kiwoom.com"},
        {"base-url": "https://api.kiwoom.com"},
    ],
)
def test_provenance_key_aliases_cannot_bypass_mock_validation(
    conflicting_provenance: dict[str, object],
) -> None:
    with pytest.raises(KiwoomMockEvidenceError, match="provenance"):
        validate_mock_response_provenance({"provenance": conflicting_provenance})


@pytest.mark.parametrize(
    "invalid_url",
    [
        "https://",
        "https:///missing-host",
        "http://mockapi.kiwoom.com",
        "https://mockapi.kiwoom.com:443",
        "https://mockapi.kiwoom.com/path",
        "http://[",
    ],
)
def test_mock_provenance_rejects_malformed_or_noncanonical_base_url(
    invalid_url: str,
) -> None:
    with pytest.raises(KiwoomMockEvidenceError, match="provenance"):
        validate_mock_response_provenance({"provenance": {"base_url": invalid_url}})


def test_mock_provenance_is_stable_and_api_specific() -> None:
    assert build_mock_provenance("kt00018") == {
        "broker": "kiwoom",
        "environment": "mock",
        "account_mode": "kiwoom_mock",
        "host": "mockapi.kiwoom.com",
        "api_id": "kt00018",
    }
