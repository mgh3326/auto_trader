"""ROB-842 final-review coordinator blockers: replay-before-freshness (F2) and
cross-process sell reservation / no-oversell (F5)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import AlpacaPaperOrderLedger
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
from app.services.alpaca_paper_submit_service import (
    AlpacaPaperSubmitCoordinator,
    build_canonical_payload,
    canonical_hash,
    derive_automated_key,
)
from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError
from app.services.brokers.alpaca.schemas import Order

pytestmark = [pytest.mark.asyncio]

_NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
_CORR = "rob842-final"


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    # Manual sell rows key their correlation on the derived manual coid
    # (rob74-crypto-…), so also clear manual keys to avoid a leftover open sell
    # polluting the account+symbol reservation across tests.
    stmt = delete(AlpacaPaperOrderLedger).where(
        AlpacaPaperOrderLedger.lifecycle_correlation_id.like(f"{_CORR}%")
        | AlpacaPaperOrderLedger.client_order_id.like("rob74-crypto-%")
        | AlpacaPaperOrderLedger.client_order_id.like("rob73-%")
    )
    await db_session.execute(stmt)
    await db_session.commit()
    yield
    await db_session.execute(stmt)
    await db_session.commit()


class FinalBroker:
    def __init__(
        self,
        *,
        position: Any = None,
        submit_error: Exception | None = None,
        delay_s: float = 0.0,
    ):
        self.submit_calls: list[Any] = []
        self.position = position
        self.submit_error = submit_error
        self._delay_s = delay_s

    async def submit_order(self, request: Any) -> Order:
        self.submit_calls.append(request)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self.submit_error is not None:
            raise self.submit_error
        return Order(
            id=f"paper-{len(self.submit_calls)}",
            client_order_id=getattr(request, "client_order_id", None),
            symbol=getattr(request, "symbol", "BTC/USD"),
            filled_qty=Decimal("0"),
            side=getattr(request, "side", "buy"),
            type=getattr(request, "type", "limit"),
            time_in_force=getattr(request, "time_in_force", "gtc"),
            status="accepted",
            limit_price=getattr(request, "limit_price", None),
        )

    async def get_position(self, symbol: str) -> Any:
        return self.position

    async def get_order_by_client_order_id(self, client_order_id: str) -> Order | None:
        return None


def _canonical(side="buy", *, qty=None, notional="10", limit="50000"):
    return build_canonical_payload(
        symbol="BTC/USD",
        side=side,
        type="limit",
        time_in_force="gtc",
        qty=Decimal(str(qty)) if qty is not None else None,
        notional=Decimal(str(notional)) if notional is not None else None,
        limit_price=Decimal(limit),
        asset_class="crypto",
    )


def _packet(canonical, corr, *, expires_at, origin="automated", **overrides):
    from app.services.alpaca_paper_submit_service import derive_client_order_id
    from app.services.paper_approval_packet import PaperApprovalPacket

    snap = f"{corr}-snap"
    if origin == "manual":
        coid = derive_client_order_id(canonical)
        corr_id = coid
        snap_id = None
    else:
        coid = derive_automated_key(
            correlation_id=corr, snapshot_id=snap, canonical=canonical
        )
        corr_id = corr
        snap_id = snap
    defaults: dict[str, Any] = {
        "signal_source": "test",
        "artifact_id": uuid.uuid4(),
        "signal_symbol": "KRW-BTC",
        "signal_venue": "upbit",
        "execution_symbol": "BTC/USD",
        "execution_venue": "alpaca_paper",
        "execution_asset_class": "crypto",
        "side": canonical["side"],
        "max_notional": Decimal("10"),
        "qty_source": "notional_estimate",
        "expected_lifecycle_step": "previewed",
        "lifecycle_correlation_id": corr_id,
        "client_order_id": coid,
        "expires_at": expires_at,
        "account_mode": "alpaca_paper",
        "origin": origin,
        "market_data_asof": _NOW - timedelta(seconds=10),
        "market_data_source": "upbit_ticker",
        "preview_payload_hash": canonical_hash(canonical),
        "snapshot_id": snap_id,
        "execution_order_type": canonical.get("type"),
        "execution_time_in_force": canonical.get("time_in_force"),
        "reference_price": Decimal("50000"),
    }
    defaults.update(overrides)
    return PaperApprovalPacket(**defaults)


# ---------------------------------------------------------------------------
# F2 — a completed/terminal order replays even after the packet expiry window
# ---------------------------------------------------------------------------
async def test_completed_order_replays_after_packet_expiry(db_session):
    corr = f"{_CORR}-replay"
    canonical = _canonical()
    packet = _packet(canonical, corr, expires_at=_NOW + timedelta(seconds=60))
    broker = FinalBroker()
    ledger = AlpacaPaperLedgerService(db_session)

    fresh = AlpacaPaperSubmitCoordinator(ledger, lambda: broker, now_fn=lambda: _NOW)
    first = await fresh.submit(packet, submit_canonical=canonical)
    assert first.status == "submitted"

    # Same intent retried LONG after the packet's freshness window elapsed.
    expired = AlpacaPaperSubmitCoordinator(
        ledger, lambda: broker, now_fn=lambda: _NOW + timedelta(hours=2)
    )
    second = await expired.submit(packet, submit_canonical=canonical)
    assert second.status == "replayed"  # not rejected/stale_packet
    assert second.success is True
    assert len(broker.submit_calls) == 1


async def test_terminal_failure_replays_after_expiry_not_stale(db_session):
    corr = f"{_CORR}-failreplay"
    canonical = _canonical()
    packet = _packet(canonical, corr, expires_at=_NOW + timedelta(seconds=60))
    broker = FinalBroker(submit_error=AlpacaPaperRequestError("bad", status_code=422))
    ledger = AlpacaPaperLedgerService(db_session)

    fresh = AlpacaPaperSubmitCoordinator(ledger, lambda: broker, now_fn=lambda: _NOW)
    first = await fresh.submit(packet, submit_canonical=canonical)
    assert first.status == "failed"

    expired = AlpacaPaperSubmitCoordinator(
        ledger, lambda: broker, now_fn=lambda: _NOW + timedelta(hours=2)
    )
    second = await expired.submit(packet, submit_canonical=canonical)
    assert second.status == "failed"
    assert second.reason_code == "broker_rejected_replayed"
    assert second.success is False
    assert len(broker.submit_calls) == 1


# ---------------------------------------------------------------------------
# F5 — different sell tokens cannot both consume the same position (no oversell)
# ---------------------------------------------------------------------------
async def test_distinct_sells_do_not_oversell_position(db_session):
    from app.core.db import AsyncSessionLocal

    corr = f"{_CORR}-oversell"
    # position = 1 share; two different intents want 0.6 and 0.7.
    c06 = _canonical("sell", qty="0.6", notional=None)
    c07 = _canonical("sell", qty="0.7", notional=None)
    p06 = _packet(
        c06,
        corr,
        expires_at=_NOW + timedelta(hours=1),
        origin="manual",
        side="sell",
        max_notional=None,
        max_qty=Decimal("1"),
        qty_source="manual_operator",
    )
    p07 = _packet(
        c07,
        corr,
        expires_at=_NOW + timedelta(hours=1),
        origin="manual",
        side="sell",
        max_notional=None,
        max_qty=Decimal("1"),
        qty_source="manual_operator",
    )
    shared = FinalBroker(
        position=SimpleNamespace(
            symbol="BTCUSD", qty=Decimal("1"), qty_available=Decimal("1")
        ),
        delay_s=0.02,
    )
    barrier = asyncio.Barrier(2)

    async def _run(packet, canonical):
        async with AsyncSessionLocal() as s:
            coord = AlpacaPaperSubmitCoordinator(
                AlpacaPaperLedgerService(s),
                lambda: shared,
                now_fn=lambda: _NOW,
                inflight_max_polls=20,
                inflight_poll_interval_s=0.02,
            )
            await barrier.wait()
            return await coord.submit(packet, submit_canonical=canonical)

    outcomes = await asyncio.gather(_run(p06, c06), _run(p07, c07))

    # Exactly one sell POSTs; the other is rejected for insufficient available qty.
    assert len(shared.submit_calls) == 1, shared.submit_calls
    submitted = [o for o in outcomes if o.status == "submitted"]
    rejected = [o for o in outcomes if o.status == "rejected"]
    assert len(submitted) == 1
    assert len(rejected) == 1
    assert rejected[0].reason_code == "qty_exceeds_available"


async def test_sequential_distinct_sells_reserve_across_calls(db_session):
    corr = f"{_CORR}-seqreserve"
    c06 = _canonical("sell", qty="0.6", notional=None)
    c07 = _canonical("sell", qty="0.7", notional=None)
    p06 = _packet(
        c06,
        corr,
        expires_at=_NOW + timedelta(hours=1),
        origin="manual",
        side="sell",
        max_notional=None,
        max_qty=Decimal("1"),
        qty_source="manual_operator",
    )
    p07 = _packet(
        c07,
        corr,
        expires_at=_NOW + timedelta(hours=1),
        origin="manual",
        side="sell",
        max_notional=None,
        max_qty=Decimal("1"),
        qty_source="manual_operator",
    )
    broker = FinalBroker(
        position=SimpleNamespace(
            symbol="BTCUSD", qty=Decimal("1"), qty_available=Decimal("1")
        )
    )
    ledger = AlpacaPaperLedgerService(db_session)
    coord = AlpacaPaperSubmitCoordinator(ledger, lambda: broker, now_fn=lambda: _NOW)

    first = await coord.submit(p06, submit_canonical=c06)
    second = await coord.submit(p07, submit_canonical=c07)

    assert first.status == "submitted"
    assert second.status == "rejected"
    assert second.reason_code == "qty_exceeds_available"  # 0.7 > (1 - 0.6)
    assert len(broker.submit_calls) == 1
