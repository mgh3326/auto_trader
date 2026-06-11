"""ROB-274 — pending_orders collector tests."""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.action_report.snapshot_backed.collectors.pending_orders import (
    PendingOrdersSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


def _request(market: str, account_scope: str) -> CollectorRequest:
    return CollectorRequest(
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
    )


@pytest.mark.asyncio
async def test_pending_orders_collector_kr_calls_kis_read_only_path():
    fake_kis = AsyncMock()
    # Real KIS domestic shape: ord_no/pdno/sll_buy_dvsn_cd/ord_qty/ord_unpr/ord_tmd/ord_dt
    fake_kis.inquire_korea_orders = AsyncMock(
        return_value=[
            {
                "ord_no": "K1",
                "pdno": "005930",
                "prdt_name": "삼성전자",
                "sll_buy_dvsn_cd": "02",  # 02 = buy
                "ord_qty": "10",
                "ord_unpr": "70000",
                "ord_dt": "20260519",
                "ord_tmd": "120000",
            },
        ]
    )
    collector = PendingOrdersSnapshotCollector(kis_client=fake_kis, upbit_client=None)
    request = _request(market="kr", account_scope="kis_live")
    results = await collector.collect(request)
    assert len(results) == 1
    payload = results[0].payload_json
    assert payload["count"] == 1
    assert payload["pending_orders"][0]["target_ref"]["broker"] == "kis"
    assert payload["pending_orders"][0]["target_ref"]["id"] == "K1"
    assert payload["pending_orders"][0]["side"] == "buy"
    assert payload["pending_orders"][0]["market"] == "kr"
    # No mutation method ever called.
    assert not fake_kis.order_korea_stock.called  # type: ignore[attr-defined]
    assert not fake_kis.cancel_korea_order.called  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_pending_orders_collector_us_calls_kis_overseas_path():
    fake_kis = AsyncMock()
    # Real KIS overseas shape: odno/pdno/sll_buy_dvsn_cd/ft_ord_qty/ft_ord_unpr3/nccs_qty/ord_dt/ord_tmd
    fake_kis.inquire_overseas_orders = AsyncMock(
        return_value=[
            {
                "odno": "U1",
                "pdno": "AAPL",
                "sll_buy_dvsn_cd": "02",
                "ft_ord_qty": "5",
                "ft_ord_unpr3": "180.5",
                "nccs_qty": "5",
                "ord_dt": "20260519",
                "ord_tmd": "120000",
            },
        ]
    )
    collector = PendingOrdersSnapshotCollector(kis_client=fake_kis, upbit_client=None)
    request = _request(market="us", account_scope="kis_live")
    results = await collector.collect(request)
    assert len(results) == 1
    payload = results[0].payload_json
    assert payload["count"] == 1
    assert payload["pending_orders"][0]["target_ref"]["broker"] == "kis"
    assert payload["pending_orders"][0]["target_ref"]["id"] == "U1"
    assert payload["pending_orders"][0]["market"] == "us"


@pytest.mark.asyncio
async def test_pending_orders_collector_crypto_flags_stale():
    fake_upbit = AsyncMock()
    placed = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=48)
    fake_upbit.fetch_open_orders = AsyncMock(
        return_value=[
            {
                "uuid": "U1",
                "market": "KRW-BTC",
                "side": "bid",
                "price": "100000000",
                "volume": "0.01",
                "remaining_volume": "0.01",
                "created_at": placed.isoformat(),
            },
        ]
    )
    collector = PendingOrdersSnapshotCollector(kis_client=None, upbit_client=fake_upbit)
    request = _request(market="crypto", account_scope="upbit_live")
    results = await collector.collect(request)
    payload = results[0].payload_json
    assert payload["pending_orders"][0]["stale"] is True
    assert payload["pending_orders"][0]["side"] == "buy"
    assert payload["pending_orders"][0]["target_ref"]["broker"] == "upbit"
    assert payload["pending_orders"][0]["market"] == "crypto"


@pytest.mark.asyncio
async def test_pending_orders_collector_crypto_not_stale_when_recent():
    fake_upbit = AsyncMock()
    placed = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1)
    fake_upbit.fetch_open_orders = AsyncMock(
        return_value=[
            {
                "uuid": "U2",
                "market": "KRW-ETH",
                "side": "ask",
                "price": "5000000",
                "volume": "0.1",
                "remaining_volume": "0.1",
                "created_at": placed.isoformat(),
            },
        ]
    )
    collector = PendingOrdersSnapshotCollector(kis_client=None, upbit_client=fake_upbit)
    results = await collector.collect(_request("crypto", "upbit_live"))
    payload = results[0].payload_json
    assert payload["pending_orders"][0]["stale"] is False
    assert payload["pending_orders"][0]["side"] == "sell"


@pytest.mark.asyncio
async def test_pending_orders_collector_fails_open_when_client_missing():
    collector = PendingOrdersSnapshotCollector(kis_client=None, upbit_client=None)
    results = await collector.collect(_request("kr", "kis_live"))
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"
    assert results[0].errors_json["reason"].startswith("kis_client_unavailable")


@pytest.mark.asyncio
async def test_pending_orders_collector_fails_open_on_broker_error():
    fake_kis = AsyncMock()
    fake_kis.inquire_korea_orders = AsyncMock(side_effect=RuntimeError("boom"))
    collector = PendingOrdersSnapshotCollector(kis_client=fake_kis, upbit_client=None)
    results = await collector.collect(_request("kr", "kis_live"))
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"
    assert "kis_fetch_failed" in results[0].errors_json["reason"]


@pytest.mark.asyncio
async def test_pending_orders_collector_us_dedupes_same_order_across_exchanges():
    """US: KIS returns the same order under NASD + NYSE + AMEX; collector dedupes by odno."""
    nasd_rows = [
        {
            "odno": "O1",
            "pdno": "AAPL",
            "sll_buy_dvsn_cd": "02",
            "ft_ord_qty": "10",
            "ft_ord_unpr3": "150",
            "nccs_qty": "10",
            "ord_dt": "20260519",
            "ord_tmd": "120000",
        },
        {
            "odno": "O2",
            "pdno": "MSFT",
            "sll_buy_dvsn_cd": "02",
            "ft_ord_qty": "5",
            "ft_ord_unpr3": "400",
            "nccs_qty": "5",
            "ord_dt": "20260519",
            "ord_tmd": "120100",
        },
    ]
    nyse_rows = [
        {
            "odno": "O1",
            "pdno": "AAPL",
            "sll_buy_dvsn_cd": "02",
            "ft_ord_qty": "10",
            "ft_ord_unpr3": "150",
            "nccs_qty": "10",
            "ord_dt": "20260519",
            "ord_tmd": "120000",
        },
    ]
    amex_rows: list[dict[str, Any]] = []

    async def mock_inquire(exchange_code: str, is_mock: bool = False):
        return {"NASD": nasd_rows, "NYSE": nyse_rows, "AMEX": amex_rows}[exchange_code]

    fake_kis = AsyncMock()
    fake_kis.inquire_overseas_orders = AsyncMock(side_effect=mock_inquire)
    collector = PendingOrdersSnapshotCollector(kis_client=fake_kis, upbit_client=None)
    request = _request(market="us", account_scope="kis_live")
    results = await collector.collect(request)

    assert len(results) == 1
    payload = results[0].payload_json
    assert payload["count"] == 2
    odnos = {r["target_ref"]["id"] for r in payload["pending_orders"]}
    assert odnos == {"O1", "O2"}
    # All three exchanges were queried.
    assert fake_kis.inquire_overseas_orders.await_count == 3


@pytest.mark.asyncio
async def test_pending_orders_collector_us_surfaces_partial_exchange_failure():
    """US: one exchange raises, others return rows — collector returns partial result with exchange_errors."""

    async def mock_inquire(exchange_code: str, is_mock: bool = False):
        if exchange_code == "NYSE":
            raise RuntimeError("nyse_outage")
        return [
            {
                "odno": f"O-{exchange_code}",
                "pdno": "AAPL",
                "sll_buy_dvsn_cd": "02",
                "ft_ord_qty": "10",
                "ft_ord_unpr3": "150",
                "nccs_qty": "10",
                "ord_dt": "20260519",
                "ord_tmd": "120000",
            },
        ]

    fake_kis = AsyncMock()
    fake_kis.inquire_overseas_orders = AsyncMock(side_effect=mock_inquire)
    collector = PendingOrdersSnapshotCollector(kis_client=fake_kis, upbit_client=None)
    request = _request(market="us", account_scope="kis_live")
    results = await collector.collect(request)

    # Two exchanges succeeded; partial result is still usable.
    assert len(results) == 1
    assert results[0].freshness_status != "unavailable"
    assert results[0].payload_json["count"] == 2  # NASD + AMEX, NYSE failed
    # Coverage surfaces the failure.
    exchange_errors = results[0].coverage_json.get("exchange_errors") or {}
    assert "NYSE" in exchange_errors
    assert "nyse_outage" in str(exchange_errors["NYSE"])


@pytest.mark.asyncio
async def test_pending_orders_collector_does_not_call_broker_mutation_methods():
    fake_kis = AsyncMock()
    fake_kis.inquire_korea_orders = AsyncMock(return_value=[])
    collector = PendingOrdersSnapshotCollector(kis_client=fake_kis, upbit_client=None)
    await collector.collect(_request("kr", "kis_live"))
    for forbidden in (
        "order_korea_stock",
        "sell_korea_stock",
        "cancel_korea_order",
        "modify_korea_order",
        "order_overseas_stock",
        "place_order",
        "cancel_order",
        "modify_order",
    ):
        attr = getattr(fake_kis, forbidden, None)
        if attr is not None:
            assert not attr.called, f"collector must not call {forbidden}"


def test_normalize_kis_kr_order_adds_expected_day_expiry_from_placed_at() -> None:
    from app.services.action_report.snapshot_backed.collectors.pending_orders import (
        _normalize_kis_order,
    )

    row = {
        "ord_no": "0011001100",
        "pdno": "005930",
        "sll_buy_dvsn_cd": "02",
        "ord_unpr": "70000",
        "ord_qty": "3",
        "nccs_qty": "3",
        "ord_dt": "20260611",
        "ord_tmd": "093000",
    }

    out = _normalize_kis_order(row, market="kr")

    assert out["placed_at"] == "2026-06-11T09:30:00+09:00"
    assert out["expected_expiry"] == "2026-06-11T20:00:00+09:00"


def test_normalize_kis_us_order_keeps_expected_expiry_unknown() -> None:
    from app.services.action_report.snapshot_backed.collectors.pending_orders import (
        _normalize_kis_order,
    )

    row = {
        "odno": "US-1",
        "pdno": "AAPL",
        "sll_buy_dvsn_cd": "02",
        "ord_unpr": "200",
        "ord_qty": "1",
        "nccs_qty": "1",
        "ord_dt": "20260611",
        "ord_tmd": "230000",
    }

    out = _normalize_kis_order(row, market="us")

    assert out["expected_expiry"] is None
