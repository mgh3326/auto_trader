from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.core.timezone import KST
from app.services.execution_ledger.normalizers import (
    MAX_SQL_INT32,
    _domestic_fill_seq,
    _normalize_kis_domestic_filled,
    _normalize_kis_overseas_filled,
    _normalize_upbit_filled,
    _overseas_fill_seq,
    _upbit_trade_fill_seq,
    normalize_upbit_order,
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


def test_kis_empty_filled_at_uses_kst_order_date_and_time() -> None:
    upsert = to_execution_ledger_upsert(
        {
            "symbol": "214150",
            "raw_symbol": "214150",
            "instrument_type": "equity_kr",
            "side": "sell",
            "price": "100000",
            "quantity": "2",
            "total_amount": "200000",
            "currency": "KRW",
            "account": "kis",
            "order_id": "rob933-empty-filled-at",
            "filled_at": "",
            "ord_dt": "20260716",
            "ord_tmd": "001728",
        }
    )

    assert upsert.filled_at == datetime(2026, 7, 16, 0, 17, 28, tzinfo=KST)


def test_kis_filled_at_rejects_trade_day_drift_from_order_date() -> None:
    with pytest.raises(ValueError, match="KST trade day"):
        to_execution_ledger_upsert(
            {
                "symbol": "214150",
                "raw_symbol": "214150",
                "instrument_type": "equity_kr",
                "side": "sell",
                "price": "100000",
                "quantity": "2",
                "total_amount": "200000",
                "currency": "KRW",
                "account": "kis",
                "order_id": "rob933-day-drift",
                "filled_at": "2026-07-15T15:17:28+00:00",
                "ord_dt": "20260715",
            }
        )


def test_malformed_filled_at_fails_closed_instead_of_using_current_time() -> None:
    with pytest.raises(ValueError, match="filled_at"):
        to_execution_ledger_upsert(
            {
                "symbol": "214150",
                "raw_symbol": "214150",
                "instrument_type": "equity_kr",
                "side": "sell",
                "price": "100000",
                "quantity": "2",
                "total_amount": "200000",
                "currency": "KRW",
                "account": "kis",
                "order_id": "rob933-malformed-filled-at",
                "filled_at": "definitely-not-a-timestamp",
            }
        )


# --- Issue 1 regression: cancel+partial fill acceptance ---


def test_upbit_normalizer_accepts_cancel_with_partial_fill() -> None:
    """Cancelled orders with executed_volume > 0 represent real fills."""
    raw = {
        "state": "cancel",
        "market": "KRW-ETH",
        "side": "bid",
        "executed_volume": "0.5",
        "price": "3000000",
        "paid_fee": "750",
        "uuid": "upbit-cancel-partial",
        "created_at": "2026-05-13T10:00:00+09:00",
    }
    normalized = _normalize_upbit_filled(raw)
    assert normalized is not None
    assert normalized["symbol"] == "ETH"
    assert normalized["side"] == "buy"
    assert abs(normalized["quantity"] - 0.5) < 1e-9


def test_upbit_normalizer_drops_cancel_with_no_fill() -> None:
    """Cancelled orders with zero executed_volume must be dropped."""
    raw = {
        "state": "cancel",
        "market": "KRW-SOL",
        "side": "ask",
        "executed_volume": "0",
        "price": "100000",
        "paid_fee": "0",
        "uuid": "upbit-cancel-empty",
        "created_at": "2026-05-13T10:00:00+09:00",
    }
    assert _normalize_upbit_filled(raw) is None


def test_upbit_normalizer_drops_unknown_state() -> None:
    """Orders in states other than done/cancel are dropped."""
    raw = {
        "state": "wait",
        "market": "KRW-BTC",
        "side": "bid",
        "executed_volume": "1.0",
        "price": "50000000",
        "paid_fee": "250",
        "uuid": "upbit-wait",
        "created_at": "2026-05-13T10:00:00+09:00",
    }
    assert _normalize_upbit_filled(raw) is None


# --- Issue 2 regression: Upbit per-trade fill mapping ---


def test_normalize_upbit_order_expands_trades_to_separate_fills() -> None:
    """Each trade in the order should become an independent fill row."""
    order = {
        "state": "done",
        "market": "KRW-BTC",
        "side": "bid",
        "executed_volume": "0.3",
        "avg_price": "50000000",
        "paid_fee": "7500",
        "uuid": "order-multi-trade",
        "created_at": "2026-05-13T09:00:00+09:00",
        "trades": [
            {
                "uuid": "trade-uuid-1",
                "volume": "0.1",
                "funds": "5000000",
                "created_at": "2026-05-13T09:00:01+09:00",
            },
            {
                "uuid": "trade-uuid-2",
                "volume": "0.2",
                "funds": "10000000",
                "created_at": "2026-05-13T09:00:02+09:00",
            },
        ],
    }
    fills = normalize_upbit_order(order)
    assert len(fills) == 2

    # fill_seq values must be distinct and stable (derived from trade uuid)
    seqs = {f["fill_seq"] for f in fills}
    assert len(seqs) == 2
    assert _upbit_trade_fill_seq("trade-uuid-1") in seqs
    assert _upbit_trade_fill_seq("trade-uuid-2") in seqs

    # Quantities and prices should match individual trades
    qty_sum = sum(f["quantity"] for f in fills)
    assert abs(qty_sum - 0.3) < 1e-9

    # Fee is split proportionally
    fee_sum = sum(f["fee"] for f in fills)
    assert abs(fee_sum - 7500) < 1e-4


def test_normalize_upbit_order_single_fill_when_no_trades() -> None:
    """Without trade detail the order becomes a single aggregate fill."""
    order = {
        "state": "done",
        "market": "KRW-BTC",
        "side": "ask",
        "executed_volume": "0.5",
        "price": "60000000",
        "paid_fee": "1500",
        "uuid": "order-no-trades",
        "created_at": "2026-05-13T09:00:00+09:00",
        "trades": [],
    }
    fills = normalize_upbit_order(order)
    assert len(fills) == 1
    assert fills[0]["fill_seq"] == 0
    assert fills[0]["side"] == "sell"
    assert abs(fills[0]["quantity"] - 0.5) < 1e-9


def test_normalize_upbit_order_cancel_partial_with_trades() -> None:
    """Cancelled order with partial trades should produce fills for each trade."""
    order = {
        "state": "cancel",
        "market": "KRW-ETH",
        "side": "bid",
        "executed_volume": "1.0",
        "avg_price": "3000000",
        "paid_fee": "3000",
        "uuid": "order-cancel-with-trade",
        "created_at": "2026-05-13T10:00:00+09:00",
        "trades": [
            {
                "uuid": "trade-cancel-1",
                "volume": "1.0",
                "funds": "3000000",
                "created_at": "2026-05-13T10:00:01+09:00",
            }
        ],
    }
    fills = normalize_upbit_order(order)
    assert len(fills) == 1
    assert abs(fills[0]["quantity"] - 1.0) < 1e-9
    assert fills[0]["fill_seq"] == _upbit_trade_fill_seq("trade-cancel-1")


def test_normalize_upbit_order_returns_empty_for_zero_volume() -> None:
    """Cancel with zero executed_volume yields no fills."""
    order = {
        "state": "cancel",
        "market": "KRW-BTC",
        "side": "ask",
        "executed_volume": "0",
        "price": "50000000",
        "paid_fee": "0",
        "uuid": "order-no-fill",
        "created_at": "2026-05-13T09:00:00+09:00",
        "trades": [],
    }
    assert normalize_upbit_order(order) == []


# --- Issue 3 regression: KIS overseas fill_seq dedup ---


def test_overseas_fill_seq_uses_ccld_seq_when_present() -> None:
    order = {
        "ccld_seq": "5",
        "ord_dt": "20260513",
        "ord_tmd": "093000",
        "ft_ccld_qty": "10",
        "odno": "KIS-1",
    }
    assert _overseas_fill_seq(order) == 5


def test_overseas_fill_seq_hashes_when_ccld_seq_missing() -> None:
    """When ccld_seq is absent a hash is produced so multiple fills don't collide on 0."""
    order_a = {
        "ord_dt": "20260513",
        "ord_tmd": "093000",
        "ft_ccld_qty": "10",
        "odno": "KIS-1",
    }
    order_b = {
        "ord_dt": "20260513",
        "ord_tmd": "093001",
        "ft_ccld_qty": "5",
        "odno": "KIS-1",
    }
    seq_a = _overseas_fill_seq(order_a)
    seq_b = _overseas_fill_seq(order_b)
    # Both must be non-negative PostgreSQL int32 values
    assert 0 <= seq_a <= MAX_SQL_INT32
    assert 0 <= seq_b <= MAX_SQL_INT32
    # Different inputs must produce different hashes
    assert seq_a != seq_b


def test_kis_overseas_normalizer_uses_hash_fill_seq_when_no_ccld_seq() -> None:
    order = {
        "pdno": "TSLA",
        "sll_buy_dvsn_cd": "02",
        "ft_ccld_qty": "2",
        "ft_ccld_unpr3": "250.00",
        "ft_ccld_amt3": "500.00",
        "odno": "us-order-2",
        "ord_dt": "20260513",
        "ord_tmd": "140000",
        "ovrs_excg_cd": "NASD",
        # ccld_seq intentionally absent
    }
    normalized = _normalize_kis_overseas_filled(order)
    assert normalized is not None
    assert 0 < normalized["fill_seq"] <= MAX_SQL_INT32  # hash, not 0


def test_kis_overseas_normalizer_two_fills_same_order_different_fill_seq() -> None:
    """Two raw rows for the same order_id but different execution times produce different fill_seq."""
    base = {
        "pdno": "AAPL",
        "sll_buy_dvsn_cd": "02",
        "ft_ccld_qty": "5",
        "ft_ccld_unpr3": "180.00",
        "ft_ccld_amt3": "900.00",
        "odno": "shared-order",
        "ovrs_excg_cd": "NASD",
    }
    row1 = {**base, "ord_dt": "20260513", "ord_tmd": "090000"}
    row2 = {**base, "ord_dt": "20260513", "ord_tmd": "091500"}

    n1 = _normalize_kis_overseas_filled(row1)
    n2 = _normalize_kis_overseas_filled(row2)
    assert n1 is not None
    assert n2 is not None
    assert n1["order_id"] == n2["order_id"]
    assert n1["fill_seq"] != n2["fill_seq"]


def test_hash_derived_fill_seq_values_fit_postgresql_integer() -> None:
    assert 0 <= _upbit_trade_fill_seq("trade-cancel-1") <= MAX_SQL_INT32
    assert (
        0
        <= _overseas_fill_seq(
            {
                "ord_dt": "20260513",
                "ord_tmd": "140000",
                "ft_ccld_qty": "2",
                "odno": "us-order-2",
            }
        )
        <= MAX_SQL_INT32
    )
    assert (
        0
        <= _domestic_fill_seq(
            {
                "ord_dt": "20260513",
                "ord_tmd": "093000",
                "ccld_tmd": "093001",
                "ccld_qty": "1",
            }
        )
        <= MAX_SQL_INT32
    )


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
