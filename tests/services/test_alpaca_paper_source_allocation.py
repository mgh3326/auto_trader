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

from app.core.db import AsyncSessionLocal
from app.models.review import AlpacaPaperOrderLedger
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
from app.services.alpaca_paper_submit_service import (
    AlpacaPaperSubmitCoordinator,
    build_canonical_payload,
    canonical_hash,
    derive_automated_key,
)
from app.services.brokers.alpaca.schemas import Order
from app.services.paper_approval_packet import PaperApprovalPacket

pytestmark = [pytest.mark.asyncio]

_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
_PREFIX = "rob845-sourcealloc-"


@pytest_asyncio.fixture(autouse=True)
async def _clean_source_allocation_rows(db_session):
    stmt = delete(AlpacaPaperOrderLedger).where(
        AlpacaPaperOrderLedger.client_order_id.like(f"{_PREFIX}%")
        | AlpacaPaperOrderLedger.lifecycle_correlation_id.like(f"{_PREFIX}%")
    )
    await db_session.execute(stmt)
    await db_session.commit()
    yield
    await db_session.execute(stmt)
    await db_session.commit()


class _Broker:
    """Complete Alpaca boundary double; persistence/concurrency stay real."""

    def __init__(self, *, submit_delay_s: float = 0.0) -> None:
        self.submit_calls: list[Any] = []
        self._submit_delay_s = submit_delay_s

    async def submit_order(self, request: Any) -> Order:
        self.submit_calls.append(request)
        if self._submit_delay_s:
            await asyncio.sleep(self._submit_delay_s)
        return Order(
            id=f"{_PREFIX}broker-{len(self.submit_calls)}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=request.qty,
            notional=request.notional,
            filled_qty=Decimal("0"),
            side=request.side,
            type=request.type,
            time_in_force=request.time_in_force,
            status="accepted",
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            filled_avg_price=None,
            submitted_at=_NOW,
            filled_at=None,
        )

    async def get_position(self, symbol: str) -> Any:
        return SimpleNamespace(
            symbol=symbol,
            qty=Decimal("10"),
            qty_available=Decimal("10"),
        )

    async def get_order_by_client_order_id(self, client_order_id: str) -> None:
        return None


class _PausedClaimLedger(AlpacaPaperLedgerService):
    """Pause one loser after its initial idempotency miss, before sell locking."""

    def __init__(self, db_session, *, target_client_order_id: str) -> None:
        super().__init__(db_session)
        self._target_client_order_id = target_client_order_id
        self._initial_read_seen = False
        self.initial_read = asyncio.Event()
        self.release_claim = asyncio.Event()

    async def get_execution_by_client_order_id(
        self, client_order_id: str
    ) -> AlpacaPaperOrderLedger | None:
        row = await super().get_execution_by_client_order_id(client_order_id)
        if (
            client_order_id == self._target_client_order_id
            and not self._initial_read_seen
        ):
            self._initial_read_seen = True
            self.initial_read.set()
        return row

    async def acquire_sell_reservation_lock(
        self, *, account_mode: str, execution_symbol: str
    ) -> None:
        await self.release_claim.wait()
        await super().acquire_sell_reservation_lock(
            account_mode=account_mode, execution_symbol=execution_symbol
        )


def _sell_canonical(qty: str) -> dict[str, Any]:
    return build_canonical_payload(
        symbol="BTC/USD",
        side="sell",
        type="limit",
        time_in_force="gtc",
        qty=Decimal(qty),
        notional=None,
        limit_price=Decimal("10"),
        asset_class="crypto",
    )


def _source_packet(
    canonical: dict[str, Any],
    *,
    source_client_order_id: str,
    nonce: str,
) -> PaperApprovalPacket:
    correlation_id = f"{_PREFIX}corr-{nonce}"
    snapshot_id = f"{_PREFIX}snapshot-{nonce}"
    return PaperApprovalPacket(
        signal_source="canonical_experiment",
        artifact_id=uuid.uuid4(),
        signal_symbol="BTCUSDT",
        signal_venue="binance_public_spot",
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        execution_asset_class="crypto",
        side="sell",
        max_qty=Decimal(str(canonical["qty"])),
        qty_source="verified_native_buy",
        expected_lifecycle_step="previewed",
        lifecycle_correlation_id=correlation_id,
        client_order_id=derive_automated_key(
            correlation_id=correlation_id,
            snapshot_id=snapshot_id,
            canonical=canonical,
        ),
        expires_at=_NOW + timedelta(minutes=5),
        account_mode="alpaca_paper",
        origin="automated",
        market_data_asof=_NOW,
        market_data_source="binance_public_spot",
        preview_payload_hash=canonical_hash(canonical),
        snapshot_id=snapshot_id,
        execution_order_type="limit",
        execution_time_in_force="gtc",
        reference_price=Decimal("10"),
        source_client_order_id=source_client_order_id,
        decision_identity_hash="a" * 64,
    )


async def _seed_source_buy(
    db_session,
    *,
    nonce: str,
    filled_qty: str = "1",
    lifecycle_state: str = "position_reconciled",
) -> str:
    client_order_id = f"{_PREFIX}source-{nonce}"
    db_session.add(
        AlpacaPaperOrderLedger(
            client_order_id=client_order_id,
            lifecycle_correlation_id=f"{_PREFIX}source-corr-{nonce}",
            record_kind="execution",
            broker="alpaca",
            account_mode="alpaca_paper",
            lifecycle_state=lifecycle_state,
            execution_symbol="BTC/USD",
            execution_venue="alpaca_paper",
            execution_asset_class="crypto",
            instrument_type=InstrumentType.crypto,
            side="buy",
            order_type="limit",
            time_in_force="gtc",
            currency="USD",
            requested_qty=Decimal(filled_qty),
            filled_qty=Decimal(filled_qty),
            order_status="filled",
            broker_order_id=f"{_PREFIX}buy-broker-{nonce}",
            submitted_at=_NOW - timedelta(minutes=2),
            reconciled_at=_NOW - timedelta(minutes=1),
            confirm_flag=True,
        )
    )
    await db_session.commit()
    db_session.expire_all()
    return client_order_id


async def _seed_source_sell(
    db_session,
    *,
    nonce: str,
    source_client_order_id: str,
    requested_qty: str,
    lifecycle_state: str,
    order_status: str | None = None,
    filled_qty: str | None = None,
    cancel_status: str | None = None,
) -> str:
    client_order_id = f"{_PREFIX}prior-sell-{nonce}"
    db_session.add(
        AlpacaPaperOrderLedger(
            client_order_id=client_order_id,
            lifecycle_correlation_id=f"{_PREFIX}prior-corr-{nonce}",
            record_kind="execution",
            broker="alpaca",
            account_mode="alpaca_paper",
            lifecycle_state=lifecycle_state,
            execution_symbol="BTC/USD",
            execution_venue="alpaca_paper",
            execution_asset_class="crypto",
            instrument_type=InstrumentType.crypto,
            side="sell",
            order_type="limit",
            time_in_force="gtc",
            currency="USD",
            requested_qty=Decimal(requested_qty),
            preview_payload={
                "symbol": "BTC/USD",
                "side": "sell",
                "source_buy_client_order_id": source_client_order_id,
            },
            order_status=order_status,
            filled_qty=Decimal(filled_qty) if filled_qty is not None else None,
            cancel_status=cancel_status,
            broker_order_id=(
                None if order_status is None else f"{_PREFIX}sell-broker-{nonce}"
            ),
            submitted_at=None if order_status is None else _NOW,
            confirm_flag=True,
        )
    )
    await db_session.commit()
    db_session.expire_all()
    return client_order_id


def _coordinator(db_session, broker: _Broker) -> AlpacaPaperSubmitCoordinator:
    return AlpacaPaperSubmitCoordinator(
        AlpacaPaperLedgerService(db_session),
        lambda: broker,
        now_fn=lambda: _NOW,
    )


async def test_sell_execution_persists_exact_source_buy_evidence(db_session):
    source_id = await _seed_source_buy(db_session, nonce="evidence")
    canonical = _sell_canonical("0.4")
    packet = _source_packet(
        canonical, source_client_order_id=source_id, nonce="evidence"
    )
    broker = _Broker()

    outcome = await _coordinator(db_session, broker).submit(
        packet, submit_canonical=canonical
    )

    assert outcome.status == "submitted"
    db_session.expire_all()
    row = await AlpacaPaperLedgerService(db_session).get_execution_by_client_order_id(
        packet.client_order_id
    )
    assert row is not None
    assert row.preview_payload is not None
    assert row.preview_payload["source_buy_client_order_id"] == source_id


async def test_filled_sell_consumes_source_and_blocks_sequential_reuse(db_session):
    source_id = await _seed_source_buy(db_session, nonce="sequential")
    broker = _Broker()
    first_canonical = _sell_canonical("1")
    first_packet = _source_packet(
        first_canonical, source_client_order_id=source_id, nonce="sequential-first"
    )
    first = await _coordinator(db_session, broker).submit(
        first_packet, submit_canonical=first_canonical
    )
    assert first.status == "submitted"
    await AlpacaPaperLedgerService(db_session).record_status(
        first_packet.client_order_id,
        {"status": "filled", "filled_qty": "1", "filled_avg_price": "10"},
    )

    second_canonical = _sell_canonical("0.1")
    second_packet = _source_packet(
        second_canonical, source_client_order_id=source_id, nonce="sequential-second"
    )
    second = await _coordinator(db_session, broker).submit(
        second_packet, submit_canonical=second_canonical
    )

    assert second.status == "rejected"
    assert second.reason_code == "qty_exceeds_source_available"
    assert len(broker.submit_calls) == 1


async def test_concurrent_partial_sells_cannot_over_allocate_one_source(db_session):
    source_id = await _seed_source_buy(db_session, nonce="concurrent")
    broker = _Broker(submit_delay_s=0.05)
    canonicals = [_sell_canonical("0.6"), _sell_canonical("0.6")]
    packets = [
        _source_packet(
            canonical,
            source_client_order_id=source_id,
            nonce=f"concurrent-{index}",
        )
        for index, canonical in enumerate(canonicals)
    ]

    async def _submit(index: int):
        async with AsyncSessionLocal() as session:
            return await _coordinator(session, broker).submit(
                packets[index], submit_canonical=canonicals[index]
            )

    outcomes = await asyncio.gather(_submit(0), _submit(1))

    assert sorted(outcome.status for outcome in outcomes) == ["rejected", "submitted"]
    rejected = next(outcome for outcome in outcomes if outcome.status == "rejected")
    assert rejected.reason_code == "qty_exceeds_source_available"
    assert len(broker.submit_calls) == 1


@pytest.mark.parametrize("dynamic_rejection", ["source", "account"])
async def test_same_token_winner_replays_before_dynamic_sell_rejection(
    db_session, dynamic_rejection: str
):
    source_id = await _seed_source_buy(db_session, nonce=f"race-{dynamic_rejection}")
    canonical = _sell_canonical("0.6")
    packet = _source_packet(
        canonical,
        source_client_order_id=source_id,
        nonce=f"race-{dynamic_rejection}",
    )
    broker = _Broker()

    async with (
        AsyncSessionLocal() as winner_session,
        AsyncSessionLocal() as loser_session,
    ):
        loser_ledger = _PausedClaimLedger(
            loser_session, target_client_order_id=packet.client_order_id
        )
        loser_coordinator = AlpacaPaperSubmitCoordinator(
            loser_ledger,
            lambda: broker,
            now_fn=lambda: _NOW,
        )
        loser_task = asyncio.create_task(
            loser_coordinator.submit(packet, submit_canonical=canonical)
        )
        await asyncio.wait_for(loser_ledger.initial_read.wait(), timeout=5)

        winner = await _coordinator(winner_session, broker).submit(
            packet, submit_canonical=canonical
        )
        if dynamic_rejection == "source":
            await _seed_source_sell(
                db_session,
                nonce="race-source-exhauster",
                source_client_order_id=source_id,
                requested_qty="1",
                lifecycle_state="filled",
                order_status="filled",
                filled_qty="1",
            )
        else:
            await _seed_source_sell(
                db_session,
                nonce="race-account-exhauster",
                source_client_order_id=f"{_PREFIX}different-source",
                requested_qty="9.5",
                lifecycle_state="submitted",
            )
        loser_ledger.release_claim.set()
        loser = await asyncio.wait_for(loser_task, timeout=5)

    assert winner.status == "submitted"
    assert loser.status == "replayed"
    assert loser.reason_code == "duplicate_submit_replayed"
    assert len(broker.submit_calls) == 1


@pytest.mark.parametrize("terminal_status", ["canceled", "rejected"])
async def test_zero_fill_terminal_sell_releases_source_allocation(
    db_session, terminal_status: str
):
    source_id = await _seed_source_buy(db_session, nonce=f"release-{terminal_status}")
    await _seed_source_sell(
        db_session,
        nonce=f"release-{terminal_status}",
        source_client_order_id=source_id,
        requested_qty="1",
        lifecycle_state="anomaly",
        order_status=terminal_status,
        filled_qty="0",
        cancel_status="canceled" if terminal_status == "canceled" else None,
    )
    canonical = _sell_canonical("1")
    packet = _source_packet(
        canonical,
        source_client_order_id=source_id,
        nonce=f"release-new-{terminal_status}",
    )
    broker = _Broker()

    outcome = await _coordinator(db_session, broker).submit(
        packet, submit_canonical=canonical
    )

    assert outcome.status == "submitted"
    assert len(broker.submit_calls) == 1


async def test_partial_fill_cancel_consumes_fill_but_releases_unfilled_qty(db_session):
    source_id = await _seed_source_buy(db_session, nonce="partial-cancel")
    await _seed_source_sell(
        db_session,
        nonce="partial-cancel",
        source_client_order_id=source_id,
        requested_qty="0.8",
        lifecycle_state="anomaly",
        order_status="canceled",
        filled_qty="0.3",
        cancel_status="canceled",
    )
    broker = _Broker()
    allowed_canonical = _sell_canonical("0.7")
    allowed_packet = _source_packet(
        allowed_canonical,
        source_client_order_id=source_id,
        nonce="partial-cancel-allowed",
    )

    allowed = await _coordinator(db_session, broker).submit(
        allowed_packet, submit_canonical=allowed_canonical
    )
    excess_canonical = _sell_canonical("0.01")
    excess_packet = _source_packet(
        excess_canonical,
        source_client_order_id=source_id,
        nonce="partial-cancel-excess",
    )
    excess = await _coordinator(db_session, broker).submit(
        excess_packet, submit_canonical=excess_canonical
    )

    assert allowed.status == "submitted"
    assert excess.status == "rejected"
    assert excess.reason_code == "qty_exceeds_source_available"
    assert len(broker.submit_calls) == 1


@pytest.mark.parametrize(
    ("prior_status", "nonce"),
    [(None, "inflight"), ("partially_filled", "partial-open")],
)
async def test_unresolved_sell_conservatively_consumes_requested_source_qty(
    db_session, prior_status: str | None, nonce: str
):
    source_id = await _seed_source_buy(db_session, nonce=nonce)
    await _seed_source_sell(
        db_session,
        nonce=nonce,
        source_client_order_id=source_id,
        requested_qty="0.6",
        lifecycle_state="submitted",
        order_status=prior_status,
        filled_qty="0.2" if prior_status == "partially_filled" else None,
    )
    canonical = _sell_canonical("0.5")
    packet = _source_packet(
        canonical, source_client_order_id=source_id, nonce=f"{nonce}-new"
    )
    broker = _Broker()

    outcome = await _coordinator(db_session, broker).submit(
        packet, submit_canonical=canonical
    )

    assert outcome.status == "rejected"
    assert outcome.reason_code == "qty_exceeds_source_available"
    assert broker.submit_calls == []


@pytest.mark.parametrize("source_lifecycle", ["closed", "final_reconciled"])
async def test_exact_source_terminal_lifecycle_fails_closed(
    db_session, source_lifecycle: str
):
    source_id = await _seed_source_buy(
        db_session,
        nonce=f"terminal-source-{source_lifecycle}",
        lifecycle_state=source_lifecycle,
    )
    canonical = _sell_canonical("0.1")
    packet = _source_packet(
        canonical,
        source_client_order_id=source_id,
        nonce=f"terminal-source-{source_lifecycle}",
    )
    broker = _Broker()

    outcome = await _coordinator(db_session, broker).submit(
        packet, submit_canonical=canonical
    )

    assert outcome.status == "rejected"
    assert outcome.reason_code == "source_not_reconciled"
    assert broker.submit_calls == []
