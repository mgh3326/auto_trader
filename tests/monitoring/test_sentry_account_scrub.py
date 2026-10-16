"""#1103: broker account identifiers must not reach Sentry events.

``sentry_sdk``'s ``include_local_variables`` option is left at its default
(on): frame ``vars`` carry the richest debugging signal for order-path
failures, and the whole-event ``_before_send`` sanitizer provably reaches
``exception.values[].stacktrace.frames[].vars``. KIS request builders keep
the account pair in a literal ``params`` dict (``CANO`` / ``ACNT_PRDT_CD``),
so these tests pin masking across every event surface — frame locals,
request data/headers, extra, contexts, breadcrumbs, transaction span data,
query strings and serialized dict/JSON text — while generic containers
(``params``) and non-secret lookalikes (``canonical_*``, ``account_mode``,
``contact_no``) keep their values.
"""

from __future__ import annotations

import pytest
from sentry_sdk.types import Breadcrumb, Event

import app.monitoring.sentry as sentry_module

_FAKE_CANO = "87654321"
_FAKE_ACNT_PRDT_CD = "01"


def _kis_order_params() -> dict[str, str]:
    """The shape ``kis/account.py``/``domestic_orders.py`` build as ``params``."""
    return {
        "CANO": _FAKE_CANO,
        "ACNT_PRDT_CD": _FAKE_ACNT_PRDT_CD,
        "PDNO": "005930",
        "ORD_DVSN": "00",
        "ORD_QTY": "1",
        "ORD_UNPR": "70000",
        "CTX_AREA_FK100": "",
    }


@pytest.mark.unit
def test_before_send_masks_kis_account_in_frame_locals_params():
    """include_local_variables path: the whole event dict goes through."""
    event: Event = {
        "exception": {
            "values": [
                {
                    "type": "HTTPStatusError",
                    "value": "502 from broker",
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": (
                                    "app/services/brokers/kis/domestic_orders.py"
                                ),
                                "vars": {
                                    "params": _kis_order_params(),
                                    "cano": _FAKE_CANO,
                                    "acnt_prdt_cd": _FAKE_ACNT_PRDT_CD,
                                    "symbol": "005930",
                                },
                            }
                        ]
                    },
                }
            ]
        },
    }

    sanitized = sentry_module._before_send(event, {})

    assert sanitized is not None
    frame_vars = sanitized["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    params = frame_vars["params"]
    assert params["CANO"] == "[Filtered]"
    assert params["ACNT_PRDT_CD"] == "[Filtered]"
    # the params container itself is kept — non-secret request fields stay
    # debuggable instead of dropping the whole dict (AC2 decision).
    assert params["PDNO"] == "005930"
    assert params["ORD_DVSN"] == "00"
    assert params["ORD_UNPR"] == "70000"
    assert frame_vars["cano"] == "[Filtered]"
    assert frame_vars["acnt_prdt_cd"] == "[Filtered]"
    assert frame_vars["symbol"] == "005930"


@pytest.mark.unit
def test_before_send_masks_account_identifiers_on_every_event_surface():
    event: Event = {
        "request": {
            "url": "https://broker.example/uapi/domestic-stock/v1/trading/order-cash",
            "data": _kis_order_params(),
            "headers": {"X-Tossinvest-Account": "17"},
        },
        "extra": {
            "kis_account_no": "87654321-01",
            "verified_act_no": "99887766",
            "broker_account_id": "42",
            "account_seq": 3,
            "accounts": [{"accountNo": "77778888", "accountSeq": 5}],
            # lookalikes that must keep their debugging value
            "canonical_job_id": "job-1",
            "account_mode": "kis_live",
            "account_scope": "kis_mock",
            "contact_no": "010-0000-0000",
        },
        "contexts": {"order": {"account_number": "123456789"}},
    }

    sanitized = sentry_module._before_send(event, {})

    assert sanitized is not None
    request = sanitized["request"]
    assert request["data"]["CANO"] == "[Filtered]"
    assert request["data"]["ACNT_PRDT_CD"] == "[Filtered]"
    assert request["data"]["PDNO"] == "005930"
    assert request["headers"]["X-Tossinvest-Account"] == "[Filtered]"

    extra = sanitized["extra"]
    assert extra["kis_account_no"] == "[Filtered]"
    assert extra["verified_act_no"] == "[Filtered]"
    assert extra["broker_account_id"] == "[Filtered]"
    assert extra["account_seq"] == "[Filtered]"
    # account collections keep their shape; only the identifier keys inside
    # each entry are masked.
    assert extra["accounts"] == [
        {"accountNo": "[Filtered]", "accountSeq": "[Filtered]"}
    ]
    assert extra["canonical_job_id"] == "job-1"
    assert extra["account_mode"] == "kis_live"
    assert extra["account_scope"] == "kis_mock"
    assert extra["contact_no"] == "010-0000-0000"

    assert sanitized["contexts"]["order"]["account_number"] == "[Filtered]"


@pytest.mark.unit
def test_before_send_scrubs_cano_in_query_string_json_and_repr_text():
    event: Event = {
        "request": {
            "url": (
                "https://broker.example/uapi/domestic-stock/v1/balance"
                "?CANO=87654321&ACNT_PRDT_CD=01&PDNO=005930"
            ),
        },
        "exception": {
            "values": [
                {
                    "type": "Error",
                    "value": (
                        'broker rejected {"CANO": "87654321", '
                        '"ACNT_PRDT_CD": "01", "PDNO": "005930"}'
                    ),
                    "stacktrace": {"frames": []},
                }
            ]
        },
        "message": "send params={'CANO': '87654321', 'ACNT_PRDT_CD': '01'}",
    }

    sanitized = sentry_module._before_send(event, {})

    assert sanitized is not None
    url = sanitized["request"]["url"]
    assert _FAKE_CANO not in url
    assert "CANO=" in url
    assert "ACNT_PRDT_CD=" in url
    assert "PDNO=005930" in url

    exc_value = sanitized["exception"]["values"][0]["value"]
    assert _FAKE_CANO not in exc_value
    assert '"PDNO": "005930"' in exc_value

    message = sanitized["message"]
    assert _FAKE_CANO not in message
    assert _FAKE_ACNT_PRDT_CD not in message


@pytest.mark.unit
def test_before_breadcrumb_and_transaction_mask_account_keys():
    crumb: Breadcrumb = {
        "category": "httplib",
        "message": "order submit",
        "data": {"params": _kis_order_params(), "act_no": "99887766"},
    }
    sanitized_crumb = sentry_module._before_breadcrumb(crumb, {})
    assert sanitized_crumb is not None
    assert sanitized_crumb["data"]["params"]["CANO"] == "[Filtered]"
    assert sanitized_crumb["data"]["params"]["PDNO"] == "005930"
    assert sanitized_crumb["data"]["act_no"] == "[Filtered]"

    event: Event = {
        "transaction": "POST /uapi/domestic-stock/v1/trading/order-cash",
        "spans": [
            {
                "op": "http.client",
                "description": ("POST /order-cash?CANO=87654321&ACNT_PRDT_CD=01"),
                "data": {
                    "http.query": "CANO=87654321&PDNO=005930",
                    "params": _kis_order_params(),
                    "mcp.request.argument.approval_hash": "tok-abc",
                },
            }
        ],
    }
    sanitized_event = sentry_module._before_send_transaction(event, {})
    assert sanitized_event is not None
    span = sanitized_event["spans"][0]
    assert _FAKE_CANO not in span["description"]
    assert "PDNO=005930" in span["data"]["http.query"]
    assert _FAKE_CANO not in span["data"]["http.query"]
    assert span["data"]["params"]["CANO"] == "[Filtered]"
    assert span["data"]["mcp.request.argument.approval_hash"] == "[Filtered]"


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "CANO",
        "cano",
        "kis_cano",
        "x-cano",
        "ACNT_PRDT_CD",
        "acnt_prdt_cd",
        "act_no",
        "verified_act_no",
        "account",
        "kr_account",
        "X-Tossinvest-Account",
        "account_no",
        "KIS_ACCOUNT_NO",
        "kis_account_no",
        "accountno",
        "accountNo",
        "account_number",
        "accountnumber",
        "account_id",
        "accountid",
        "broker_account_id",
        "account_seq",
        "accountseq",
        "accountSeq",
        "acct_no",
        "acctno",
        "acctnumber",
        "acntno",
        "acntnumber",
        "acntid",
        "approval_key",
        "approval_hash",
        "approvalhash",
        "APCA-API-KEY-ID",
        "x-api-key",
        "mcp.request.argument.approval_hash",
        "mcp.request.argument.cano",
    ],
)
def test_is_sensitive_key_masks_account_and_credential_keys(key: str):
    assert sentry_module._is_sensitive_key(key) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        # substring collisions that boundary matching must not mask
        "canonical",
        "canonical_job_id",
        "canonical_sha256",
        "volcano",
        "exact_nonempty",
        "exact_match",
        "artifact_not_found",
        "contact_no",
        # lane/enum account metadata — useful, not secret
        "account_mode",
        "account_scope",
        "account_type",
        "accounts",
        "acct_type",
        "acctinfo",
        "approval_required",
        "approval_window",
        # generic containers — interior keys are masked, not the container
        "params",
        "data",
        "body",
        "headers",
        "symbol",
        "PDNO",
        "ORD_DVSN",
        "tr_id",
        "tr_key",
        "custtype",
        "session",
    ],
)
def test_is_sensitive_key_keeps_non_secret_lookalikes(key: str):
    assert sentry_module._is_sensitive_key(key) is False
