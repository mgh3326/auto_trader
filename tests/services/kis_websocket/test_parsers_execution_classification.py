import logging

import pytest

from app.services.kis_websocket import KISExecutionWebSocket


@pytest.fixture
def client():
    return KISExecutionWebSocket(on_execution=lambda x: x, mock_mode=True)


# ---- _classify_overseas_execution_status: decision table ----


@pytest.mark.parametrize(
    "kw, expected",
    [
        # reject wins over cancel/fill signals
        (
            {
                "rfus_yn": "1",
                "rctf_cls": "2",
                "acpt_yn": "3",
                "cntg_yn": "2",
                "filled_qty": 10,
                "filled_price": 5,
                "order_qty": 10,
            },
            "rejected",
        ),
        # canceled via rctf_cls
        (
            {
                "rfus_yn": "0",
                "rctf_cls": "2",
                "acpt_yn": "1",
                "cntg_yn": "2",
                "filled_qty": 0,
                "filled_price": 0,
                "order_qty": 10,
            },
            "canceled",
        ),
        # canceled via acpt_yn
        (
            {
                "rfus_yn": "0",
                "rctf_cls": "0",
                "acpt_yn": "3",
                "cntg_yn": "2",
                "filled_qty": 0,
                "filled_price": 0,
                "order_qty": 10,
            },
            "canceled",
        ),
        # cntg_yn != "2" short-circuits to order_notice before qty/price inspection
        (
            {
                "rfus_yn": "0",
                "rctf_cls": "0",
                "acpt_yn": "1",
                "cntg_yn": "1",
                "filled_qty": 10,
                "filled_price": 5,
                "order_qty": 10,
            },
            "order_notice",
        ),
        # partial: 0 < filled < order
        (
            {
                "rfus_yn": "0",
                "rctf_cls": "0",
                "acpt_yn": "1",
                "cntg_yn": "2",
                "filled_qty": 5,
                "filled_price": 175.25,
                "order_qty": 10,
            },
            "partial",
        ),
        # filled: filled == order
        (
            {
                "rfus_yn": "0",
                "rctf_cls": "0",
                "acpt_yn": "1",
                "cntg_yn": "2",
                "filled_qty": 10,
                "filled_price": 248.5,
                "order_qty": 10,
            },
            "filled",
        ),
        # filled when order_qty == 0 (partial branch skipped)
        (
            {
                "rfus_yn": "0",
                "rctf_cls": "0",
                "acpt_yn": "1",
                "cntg_yn": "2",
                "filled_qty": 10,
                "filled_price": 248.5,
                "order_qty": 0,
            },
            "filled",
        ),
        # invalid_fill: price 0
        (
            {
                "rfus_yn": "0",
                "rctf_cls": "0",
                "acpt_yn": "1",
                "cntg_yn": "2",
                "filled_qty": 10,
                "filled_price": 0,
                "order_qty": 10,
            },
            "invalid_fill",
        ),
        # invalid_fill: qty 0
        (
            {
                "rfus_yn": "0",
                "rctf_cls": "0",
                "acpt_yn": "1",
                "cntg_yn": "2",
                "filled_qty": 0,
                "filled_price": 5,
                "order_qty": 10,
            },
            "invalid_fill",
        ),
        # exact-string guard: "01" is NOT a reject; falls through to filled
        (
            {
                "rfus_yn": "01",
                "rctf_cls": "0",
                "acpt_yn": "1",
                "cntg_yn": "2",
                "filled_qty": 10,
                "filled_price": 5,
                "order_qty": 10,
            },
            "filled",
        ),
    ],
)
def test_classify_overseas_execution_status(client, kw, expected):
    assert client._classify_overseas_execution_status(**kw) == expected


# ---- _parse_execution_payload: positional + numeric heuristic ----


def test_parse_payload_positional_slots(client):
    # fields[3]=price, fields[4]=qty, fields[5]=amount
    fields = ["x", "y", "z", "150.5", "10", "1600"]
    out = client._parse_execution_payload(fields, "unknown", "")
    assert out["filled_price"] == pytest.approx(150.5)
    assert out["filled_qty"] == pytest.approx(10.0)
    # amount comes from slot[5], NOT price*qty (=1505)
    assert out["filled_amount"] == pytest.approx(1600.0)


def test_parse_payload_amount_derived_when_slot_missing(client):
    fields = ["x", "y", "z", "150.5", "10"]  # len 5, no slot[5]
    out = client._parse_execution_payload(fields, "unknown", "")
    assert out["filled_amount"] == pytest.approx(1505.0)  # 150.5 * 10


def test_parse_payload_max_min_rescue_when_positional_zero(client):
    # positional slots are "0" -> fall back to max=price, min=qty
    fields = ["a", "b", "c", "0", "0", "0", "248.5", "5"]
    out = client._parse_execution_payload(fields, "unknown", "")
    assert out["filled_price"] == pytest.approx(248.5)  # max positive
    assert out["filled_qty"] == pytest.approx(5.0)  # min positive


def test_parse_payload_field_shift_misassigns(client):
    # Documents the fragility: prepending one field shifts price/qty slots.
    base = ["x", "y", "z", "150.5", "10", "1600"]
    shifted = ["PREPENDED", *base]  # now slot[3]="z", slot[4]="150.5"
    out = client._parse_execution_payload(shifted, "unknown", "")
    # slot[3]="z" -> non-numeric -> price falls to max-rescue; qty from slot[4]=150.5
    # Assert the shifted result does NOT equal the correct (150.5, 10) mapping.
    assert not (
        out["filled_price"] == pytest.approx(150.5)
        and out["filled_qty"] == pytest.approx(10.0)
    )


def test_parse_payload_kv_gate_returns_empty_for_us(client):
    # market in {kr,us} with positional fields and no `k=v` token -> {}
    out = client._parse_execution_payload(["005930", "10", "70000"], "us", "")
    assert out == {}


# ---- is_execution_event: overseas reject-logging ----


@pytest.mark.parametrize(
    "data, expected_return, expect_error_log",
    [
        ({"tr_code": "H0GSCNI0", "execution_status": "filled"}, True, False),
        ({"tr_code": "H0GSCNI0", "execution_status": "partial"}, True, False),
        ({"tr_code": "H0GSCNI9", "execution_status": "filled"}, True, False),  # mock TR
        ({"tr_code": "H0GSCNI0", "execution_status": "rejected"}, False, True),
        ({"tr_code": "H0GSCNI0", "execution_status": "order_notice"}, False, True),
        ({"tr_code": "H0GSCNI0", "execution_status": "invalid_fill"}, False, True),
        # legacy path (no execution_status): fill_yn=2 + qty/price>0 -> accept
        (
            {
                "tr_code": "H0GSCNI0",
                "fill_yn": "2",
                "filled_qty": 5,
                "filled_price": 150,
            },
            True,
            False,
        ),
        # legacy reject: fill_yn != 2
        (
            {
                "tr_code": "H0GSCNI0",
                "fill_yn": "1",
                "filled_qty": 5,
                "filled_price": 150,
            },
            False,
            True,
        ),
        # legacy reject: qty 0
        (
            {
                "tr_code": "H0GSCNI0",
                "fill_yn": "2",
                "filled_qty": 0,
                "filled_price": 150,
            },
            False,
            True,
        ),
        # type=error short-circuits before overseas branch (no log)
        (
            {"tr_code": "H0GSCNI0", "type": "error", "execution_status": "filled"},
            False,
            False,
        ),
    ],
)
def test_is_execution_event_overseas_reject_logging(
    client, caplog, data, expected_return, expect_error_log
):
    with caplog.at_level(
        logging.ERROR, logger="app.services.kis_websocket_internal.parsers"
    ):
        result = client._is_execution_event(data)
    assert result is expected_return
    logged = any("REJECTED" in r.message for r in caplog.records)
    assert logged is expect_error_log
