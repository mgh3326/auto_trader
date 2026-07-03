# tests/test_trade_retrospective_pending.py
"""ROB-647 — trade_retrospective_pending due-list across 3 live ledgers."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeRetrospective,
)
from app.services.trade_journal import trade_retrospective_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    for model in (
        TradeRetrospective,
        KISLiveOrderLedger,
        LiveOrderLedger,
        TossLiveOrderLedger,
    ):
        await db_session.execute(delete(model))
    await db_session.commit()


def _kis_row(*, order_no, status="filled", report_item_uuid=None):
    return KISLiveOrderLedger(
        trade_date=now_kst(),
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        account_mode="kis_live",
        broker="kis",
        status=status,
        lifecycle_state=status,
        order_no=order_no,
        quantity=Decimal("1"),
        report_item_uuid=report_item_uuid,
    )


def _generic_row(*, order_no, market="us", account_scope="kis_live", status="filled"):
    return LiveOrderLedger(
        trade_date=now_kst(),
        broker="kis" if account_scope == "kis_live" else "upbit",
        account_scope=account_scope,
        market=market,
        symbol="AAPL" if market == "us" else "KRW-BTC",
        side="buy",
        order_kind="limit",
        status=status,
        lifecycle_state=status,
        order_no=order_no,
    )


def _toss_row(*, client_order_id, broker_order_id=None, status="filled", op="place"):
    return TossLiveOrderLedger(
        trade_date=now_kst(),
        broker="toss",
        account_mode="toss_live",
        operation_kind=op,
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        status=status,
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
    )


@pytest.mark.asyncio
async def test_lists_terminal_rows_from_all_three_ledgers(db_session: AsyncSession):
    db_session.add_all(
        [
            _kis_row(order_no="K1"),
            _generic_row(order_no="G1"),
            _toss_row(client_order_id="T1", broker_order_id="TB1"),
        ]
    )
    await db_session.commit()

    result = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    ledgers = {p["ledger"] for p in result["pending"]}
    assert ledgers == {"kis_live", "live", "toss_live"}
    assert result["total_pending"] == 3
    # suggested correlation ids are namespaced per ledger
    by_ledger = {p["ledger"]: p for p in result["pending"]}
    assert by_ledger["kis_live"]["suggested_correlation_id"] == "kis_live:K1"
    assert by_ledger["live"]["suggested_correlation_id"] == "live:G1"
    assert by_ledger["toss_live"]["suggested_correlation_id"] == "toss_live:TB1"


@pytest.mark.asyncio
async def test_non_terminal_rows_excluded(db_session: AsyncSession):
    db_session.add_all(
        [
            _kis_row(order_no="K1", status="accepted"),
            _generic_row(order_no="G1", status="pending"),
            _toss_row(client_order_id="T1", status="partial"),
        ]
    )
    await db_session.commit()
    result = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    assert result["total_pending"] == 0


@pytest.mark.asyncio
async def test_covered_by_suggested_correlation_id(db_session: AsyncSession):
    db_session.add(_kis_row(order_no="K1"))
    await db_session.commit()
    # A retrospective written with the tool-suggested correlation_id covers it.
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        correlation_id="kis_live:K1",
    )
    await db_session.commit()

    result = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    assert result["total_pending"] == 0


@pytest.mark.asyncio
async def test_covered_by_report_item_uuid(db_session: AsyncSession):
    rid = uuid.uuid4()
    db_session.add(_kis_row(order_no="K1", report_item_uuid=rid))
    await db_session.commit()
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        report_item_uuid=str(rid),
    )
    await db_session.commit()

    result = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    assert result["total_pending"] == 0


@pytest.mark.asyncio
async def test_account_mode_filter(db_session: AsyncSession):
    db_session.add_all(
        [
            _kis_row(order_no="K1"),
            _generic_row(order_no="G1", market="crypto", account_scope="upbit_live"),
            _toss_row(client_order_id="T1", broker_order_id="TB1"),
        ]
    )
    await db_session.commit()

    result = await svc.build_retrospective_pending(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="toss_live",
    )
    assert {p["ledger"] for p in result["pending"]} == {"toss_live"}

    result_upbit = await svc.build_retrospective_pending(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="upbit_live",
    )
    assert {p["ledger"] for p in result_upbit["pending"]} == {"live"}
    assert result_upbit["pending"][0]["market"] == "crypto"


@pytest.mark.asyncio
async def test_toss_non_place_operations_excluded(db_session: AsyncSession):
    db_session.add(
        _toss_row(client_order_id="T-cancel", broker_order_id="TBc", op="cancel")
    )
    await db_session.commit()
    result = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    assert result["total_pending"] == 0


@pytest.mark.asyncio
async def test_rejected_order_without_order_no_uses_row_id(db_session: AsyncSession):
    db_session.add(_kis_row(order_no=None, status="rejected"))
    await db_session.commit()
    result = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    assert result["total_pending"] == 1
    entry = result["pending"][0]
    assert entry["order_ref"] is None
    assert entry["suggested_correlation_id"].startswith("kis_live:id:")


@pytest.mark.asyncio
async def test_cancelled_excluded_by_default(db_session: AsyncSession):
    db_session.add_all(
        [
            _kis_row(order_no="K-FILL", status="filled"),
            _kis_row(order_no="K-CANCEL", status="cancelled"),
        ]
    )
    await db_session.commit()

    result = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    refs = {p["suggested_correlation_id"] for p in result["pending"]}
    assert refs == {"kis_live:K-FILL"}
    assert result["total_pending"] == 1
    assert result["include_cancelled"] is False
    assert result["excluded_by_filter"] == {"cancelled": 1}


@pytest.mark.asyncio
async def test_include_cancelled_restores_cancel_rows(db_session: AsyncSession):
    db_session.add_all(
        [
            _kis_row(order_no="K-FILL", status="filled"),
            _kis_row(order_no="K-CANCEL", status="cancelled"),
        ]
    )
    await db_session.commit()

    result = await svc.build_retrospective_pending(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        include_cancelled=True,
    )
    refs = {p["suggested_correlation_id"] for p in result["pending"]}
    assert refs == {"kis_live:K-FILL", "kis_live:K-CANCEL"}
    assert result["total_pending"] == 2
    assert result["include_cancelled"] is True
    assert result["excluded_by_filter"] == {"cancelled": 0}


@pytest.mark.asyncio
async def test_anomaly_and_rejected_kept_by_default(db_session: AsyncSession):
    db_session.add_all(
        [
            _kis_row(order_no="K-ANOM", status="anomaly"),
            _kis_row(order_no="K-REJ", status="rejected"),
        ]
    )
    await db_session.commit()

    result = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    refs = {p["suggested_correlation_id"] for p in result["pending"]}
    assert refs == {"kis_live:K-ANOM", "kis_live:K-REJ"}
    assert result["excluded_by_filter"] == {"cancelled": 0}


@pytest.mark.asyncio
async def test_toss_cancel_family_excluded_by_default(db_session: AsyncSession):
    db_session.add_all(
        [
            _toss_row(client_order_id="T-CR", status="cancel_rejected"),
            _toss_row(client_order_id="T-RR", status="replace_rejected"),
            _toss_row(
                client_order_id="T-FILL", broker_order_id="TB-FILL", status="filled"
            ),
        ]
    )
    await db_session.commit()

    default = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    assert {p["ledger"] for p in default["pending"]} == {"toss_live"}
    assert default["total_pending"] == 1  # only the filled row
    assert default["excluded_by_filter"] == {"cancelled": 2}

    opted_in = await svc.build_retrospective_pending(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        include_cancelled=True,
    )
    assert opted_in["total_pending"] == 3
    assert opted_in["excluded_by_filter"] == {"cancelled": 0}
