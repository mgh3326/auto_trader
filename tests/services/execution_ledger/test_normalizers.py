from __future__ import annotations

from decimal import Decimal

from app.services.execution_ledger.normalizers import (
    _normalize_kis_domestic_filled,
    _normalize_kis_overseas_filled,
    _normalize_upbit_filled,
    to_execution_ledger_upsert,
)


def test_upbit_normalizer_redacts_and_maps_execution_upsert() -> None:
    raw = {
        "state": "done",
        "market": "KRW-BTC",
        "side": "bid",
        "executed_volume": "0.01",
        "price": "100000000",
        "paid_fee": "500",
        "uuid": "upbit-order-1",
        "created_at": "2026-05-13T09:00:00+09:00",
        "authorization": "Bearer secret",
    }

    normalized = _normalize_upbit_filled(raw)

    assert normalized is not None
    assert normalized["symbol"] == "BTC"
    assert normalized["instrument_type"] == "crypto"
    assert normalized["side"] == "buy"
    assert normalized["raw_payload_json"]["authorization"] == "[REDACTED]"

    upsert = to_execution_ledger_upsert(normalized)
    assert upsert.broker == "upbit"
    assert upsert.broker_order_id == "upbit-order-1"
    assert upsert.fill_seq == 0
    assert upsert.filled_notional == Decimal("1000000.0")


def test_kis_domestic_normalizer_maps_sell_execution() -> None:
    normalized = _normalize_kis_domestic_filled(
        {
            "pdno": "005930",
            "sll_buy_dvsn_cd": "01",
            "ccld_qty": "2",
            "ccld_unpr": "80000",
            "ccld_amt": "160000",
            "ord_no": "12345",
            "ord_dt": "20260513",
            "ord_tmd": "093000",
            "ccld_seq": "7",
        }
    )

    assert normalized is not None
    assert normalized["symbol"] == "005930"
    assert normalized["instrument_type"] == "equity_kr"
    assert normalized["side"] == "sell"
    assert normalized["fill_seq"] == 7
    assert normalized["currency"] == "KRW"


def test_kis_overseas_normalizer_maps_us_execution() -> None:
    normalized = _normalize_kis_overseas_filled(
        {
            "pdno": "AAPL",
            "sll_buy_dvsn_cd": "02",
            "ft_ccld_qty": "3",
            "ft_ccld_unpr3": "190.12",
            "ft_ccld_amt3": "570.36",
            "odno": "us-order-1",
            "ord_dt": "20260513",
            "ord_tmd": "093000",
            "ccld_seq": "2",
            "ovrs_excg_cd": "NASD",
        }
    )

    assert normalized is not None
    assert normalized["symbol"] == "AAPL"
    assert normalized["instrument_type"] == "equity_us"
    assert normalized["side"] == "buy"
    assert normalized["fill_seq"] == 2
    assert normalized["currency"] == "USD"
