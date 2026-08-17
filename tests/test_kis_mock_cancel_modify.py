"""ROB-406 — kis_mock cancel/modify via ledger (no TTTC8036R inquiry)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.mcp_server.tooling.kis_mock_ledger as kml
import app.mcp_server.tooling.orders_modify_cancel as omc
from app.models.review import KISMockOrderLedger


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
async def test_mock_cancel_without_a_grant_is_refused_and_calls_no_broker(
    db_session: AsyncSession, monkeypatch
):
    """ROB-1263 B-6: a follow-up needs current grant *and* claim ownership.

    This used to confirm the cancel. It no longer can: with the lane at
    ``AUTO_READY_BLOCKED_BY_LIFECYCLE`` there is no J3A grant and no durable
    claim for the ledger row to own, so the only contract-legal outcome is to
    hold and make exactly zero mutation calls. The previous expectation is
    preserved above the assertion so the behaviour change is legible in review.
    """
    row = await _seed(db_session, orgno="00950")
    created: list[dict] = []

    def _client(**kwargs):
        created.append(kwargs)
        return _FakeKisCancelOK()

    monkeypatch.setattr(omc, "_create_kis_client", _client)

    result = await omc._cancel_kis_domestic(row.order_no, None, is_mock=True)

    assert created == []  # zero broker construction, therefore zero transport
    assert result["success"] is False
    assert result["reason_code"] == "claim_followup_not_authorized"
    assert result["claim_released"] is False
    await db_session.refresh(row)
    assert row.lifecycle_state != "cancelled"  # the claim is retained


@pytest.mark.asyncio
async def test_mock_cancel_refusal_precedes_the_broker_unsupported_branch(
    db_session: AsyncSession, monkeypatch
):
    """The soft-cancel branch is unreachable while the follow-up is unauthorized."""
    row = await _seed(db_session, orgno="00950")
    monkeypatch.setattr(
        omc,
        "_create_kis_client",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("unauthorized follow-up must not construct a client")
        ),
    )

    result = await omc._cancel_kis_domestic(row.order_no, None, is_mock=True)

    assert result["success"] is False
    assert result["reason_code"] == "claim_followup_not_authorized"
    assert result.get("soft_cancelled") is None
    await db_session.refresh(row)
    assert row.lifecycle_state != "cancelled"


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
async def test_mock_cancel_unknown_order_fails(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda *, is_mock: _FakeKisCancelOK()
    )
    result = await omc._cancel_kis_domestic("NO-SUCH", None, is_mock=True)
    assert result["success"] is False
    assert "ledger" in result["error"]


class _FakeKisModifyOK:
    async def modify_korea_order(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return {"odno": "REV-2"}

    async def inquire_korea_orders(self, *a, **k):
        raise AssertionError("inquire_korea_orders called in mock modify path")


class _FakeKisModifyUnsupported:
    async def modify_korea_order(self, *args, **kwargs):
        raise RuntimeError("APBK0918 not available in mock mode")

    async def inquire_korea_orders(self, *a, **k):
        raise AssertionError("inquire_korea_orders called in mock modify path")


@pytest.mark.asyncio
async def test_mock_modify_success_updates_ledger(
    db_session: AsyncSession, monkeypatch
):
    row = await _seed(db_session, orgno="00950", price="70000", quantity="10")
    fake = _FakeKisModifyOK()
    monkeypatch.setattr(omc, "_create_kis_client", lambda *, is_mock: fake)

    result = await omc._modify_kis_domestic(
        row.order_no,
        "005930",
        "equity_kr",
        new_price=71000.0,
        new_quantity=8.0,
        dry_run=False,
        is_mock=True,
    )

    assert result["success"] is True
    assert result["status"] == "modified"
    await db_session.refresh(row)
    assert row.price == Decimal("71000")
    assert row.quantity == Decimal("8")


@pytest.mark.asyncio
async def test_mock_modify_unsupported_fails_closed(
    db_session: AsyncSession, monkeypatch
):
    row = await _seed(db_session, orgno="00950")
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda *, is_mock: _FakeKisModifyUnsupported()
    )

    result = await omc._modify_kis_domestic(
        row.order_no,
        "005930",
        "equity_kr",
        new_price=71000.0,
        new_quantity=None,
        dry_run=False,
        is_mock=True,
    )

    assert result["success"] is False
    assert result["mock_unsupported"] is True
    await db_session.refresh(row)
    assert row.lifecycle_state == "accepted"  # unchanged, not soft-modified
    assert row.price == Decimal("70000")  # unchanged


@pytest.mark.asyncio
async def test_live_cancel_still_uses_inquire(monkeypatch):
    """is_mock=False must keep using inquire_korea_orders (unchanged path)."""
    called = {"inquire": False}

    class _FakeLive:
        async def inquire_korea_orders(self, *a, **k):
            called["inquire"] = True
            return []

        async def cancel_korea_order(self, **kwargs):
            return {"odno": "L-1", "ord_tmd": "0900"}

    monkeypatch.setattr(omc, "_create_kis_client", lambda *, is_mock: _FakeLive())
    # symbol 미제공 → live 경로는 inquire로 심볼 조회 시도
    await omc._cancel_kis_domestic("LIVE-1", None, is_mock=False)
    assert called["inquire"] is True
