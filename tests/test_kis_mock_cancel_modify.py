"""ROB-406 — kis_mock cancel/modify via ledger (no TTTC8036R inquiry)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import KISMockOrderLedger
import app.mcp_server.tooling.kis_mock_ledger as kml


async def _seed(db_session: AsyncSession, **overrides) -> KISMockOrderLedger:
    row = KISMockOrderLedger(
        trade_date=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        symbol=overrides.get("symbol", "005930"),
        instrument_type="equity_kr",
        side=overrides.get("side", "buy"),
        order_type="limit",
        quantity=Decimal(overrides.get("quantity", "10")),
        price=Decimal(overrides.get("price", "70000")),
        amount=Decimal("700000"),
        currency="KRW",
        order_no=overrides.get("order_no", f"MOCK-{uuid4()}"),
        krx_fwdg_ord_orgno=overrides.get("orgno", "00950"),
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="accepted",
        holdings_baseline_qty=Decimal("0"),
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def test_resolve_mock_order_for_cancel_returns_fields(
    db_session: AsyncSession,
):
    row = await _seed(db_session, orgno="00950", side="buy")
    resolved = await kml.resolve_mock_order_for_cancel(row.order_no)
    assert resolved is not None
    assert resolved["ledger_id"] == row.id
    assert resolved["symbol"] == "005930"
    assert resolved["krx_fwdg_ord_orgno"] == "00950"
    assert resolved["side"] == "buy"


@pytest.mark.asyncio
async def test_resolve_mock_order_for_cancel_missing_returns_none(
    db_session: AsyncSession,
):
    assert await kml.resolve_mock_order_for_cancel("NOPE") is None


@pytest.mark.asyncio
async def test_mark_cancelled_sets_state_and_flag(db_session: AsyncSession):
    row = await _seed(db_session)
    await kml.mark_kis_mock_order_cancelled(
        ledger_id=row.id, broker_confirmed=False, detail={"reason": "x"}
    )
    await db_session.refresh(row)
    assert row.lifecycle_state == "cancelled"
    assert row.last_reconcile_detail["broker_cancel_confirmed"] is False


import app.mcp_server.tooling.orders_modify_cancel as omc


class _FakeKisCancelOK:
    async def cancel_korea_order(self, **kwargs):
        self.kwargs = kwargs
        return {"odno": "REV-1", "ord_tmd": "0901", "msg": "ok"}

    async def inquire_korea_orders(self, *a, **k):  # must NOT be called
        raise AssertionError("inquire_korea_orders called in mock cancel path")


class _FakeKisCancelUnsupported:
    async def cancel_korea_order(self, **kwargs):
        raise RuntimeError("APBK0918 not available in mock mode")

    async def inquire_korea_orders(self, *a, **k):
        raise AssertionError("inquire_korea_orders called in mock cancel path")


class _FakeKisCancelError:
    async def cancel_korea_order(self, **kwargs):
        raise RuntimeError("APBK1234 already filled order")

    async def inquire_korea_orders(self, *a, **k):
        raise AssertionError("inquire_korea_orders called in mock cancel path")


@pytest.mark.asyncio
async def test_mock_cancel_success_confirms_and_cancels(
    db_session: AsyncSession, monkeypatch
):
    row = await _seed(db_session, orgno="00950")
    fake = _FakeKisCancelOK()
    monkeypatch.setattr(omc, "_create_kis_client", lambda *, is_mock: fake)

    result = await omc._cancel_kis_domestic(row.order_no, None, is_mock=True)

    assert result["success"] is True
    assert result["broker_cancel_confirmed"] is True
    assert fake.kwargs["krx_fwdg_ord_orgno"] == "00950"
    assert fake.kwargs["is_mock"] is True
    await db_session.refresh(row)
    assert row.lifecycle_state == "cancelled"


@pytest.mark.asyncio
async def test_mock_cancel_unsupported_soft_cancels(
    db_session: AsyncSession, monkeypatch
):
    row = await _seed(db_session, orgno="00950")
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda *, is_mock: _FakeKisCancelUnsupported()
    )

    result = await omc._cancel_kis_domestic(row.order_no, None, is_mock=True)

    assert result["success"] is True
    assert result["broker_cancel_confirmed"] is False
    assert result["mock_unsupported"] is True
    assert "warning" in result
    await db_session.refresh(row)
    assert row.lifecycle_state == "cancelled"


@pytest.mark.asyncio
async def test_mock_cancel_other_error_surfaces_no_soft_cancel(
    db_session: AsyncSession, monkeypatch
):
    row = await _seed(db_session, orgno="00950")
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda *, is_mock: _FakeKisCancelError()
    )

    result = await omc._cancel_kis_domestic(row.order_no, None, is_mock=True)

    assert result["success"] is False
    assert result.get("broker_cancel_confirmed") is False
    await db_session.refresh(row)
    assert row.lifecycle_state == "accepted"  # unchanged


@pytest.mark.asyncio
async def test_mock_cancel_unknown_order_fails(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda *, is_mock: _FakeKisCancelOK()
    )
    result = await omc._cancel_kis_domestic("NO-SUCH", None, is_mock=True)
    assert result["success"] is False
    assert "ledger" in result["error"]
