from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete

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


@pytest_asyncio.fixture(autouse=True)
async def _clean_rob845_rows(db_session):
    stmt = delete(AlpacaPaperOrderLedger).where(
        (AlpacaPaperOrderLedger.client_order_id == "source-buy-1")
        | AlpacaPaperOrderLedger.lifecycle_correlation_id.like("rob845-alpaca-sell%")
        | AlpacaPaperOrderLedger.lifecycle_correlation_id.in_({"d" * 64, "e" * 64})
    )
    await db_session.execute(stmt)
    await db_session.commit()
    yield
    await db_session.execute(stmt)
    await db_session.commit()


class _Broker:
    def __init__(self) -> None:
        self.submit_calls: list[Any] = []

    async def submit_order(self, request: Any) -> Order:
        self.submit_calls.append(request)
        return Order(
            id="alpaca-paper-sell-1",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            filled_qty=Decimal("0"),
            side=request.side,
            type=request.type,
            time_in_force=request.time_in_force,
            status="accepted",
            limit_price=request.limit_price,
        )

    async def get_position(self, symbol: str) -> Any:
        return SimpleNamespace(
            symbol=symbol,
            qty=Decimal("0.002"),
            qty_available=Decimal("0.002"),
        )

    async def get_order_by_client_order_id(self, client_order_id: str) -> None:
        return None


def _sell_canonical(qty: str = "0.001") -> dict[str, Any]:
    return build_canonical_payload(
        symbol="BTC/USD",
        side="sell",
        type="limit",
        time_in_force="gtc",
        qty=Decimal(qty),
        notional=None,
        limit_price=Decimal("50000"),
        asset_class="crypto",
    )


def _source_bound_packet(
    canonical: dict[str, Any], *, source_client_order_id: str | None
) -> PaperApprovalPacket:
    correlation_id = "rob845-alpaca-sell"
    snapshot_id = "canonical-snapshot-1"
    coid = derive_automated_key(
        correlation_id=correlation_id,
        snapshot_id=snapshot_id,
        canonical=canonical,
    )
    return PaperApprovalPacket(
        signal_source="canonical_experiment",
        artifact_id=uuid.uuid4(),
        signal_symbol="BTCUSDT",
        signal_venue="binance_public_spot",
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        execution_asset_class="crypto",
        side="sell",
        max_notional=Decimal("50"),
        qty_source="verified_native_buy",
        expected_lifecycle_step="previewed",
        lifecycle_correlation_id=correlation_id,
        client_order_id=coid,
        expires_at=_NOW + timedelta(minutes=5),
        account_mode="alpaca_paper",
        origin="automated",
        market_data_asof=_NOW,
        market_data_source="binance_public_spot",
        preview_payload_hash=canonical_hash(canonical),
        snapshot_id=snapshot_id,
        execution_order_type="limit",
        execution_time_in_force="gtc",
        reference_price=Decimal("50000"),
        source_client_order_id=source_client_order_id,
        decision_identity_hash="c" * 64 if source_client_order_id else None,
    )


async def _seed_source_buy(db_session, *, client_order_id: str) -> None:
    db_session.add(
        AlpacaPaperOrderLedger(
            client_order_id=client_order_id,
            lifecycle_correlation_id="source-correlation",
            record_kind="execution",
            broker="alpaca",
            account_mode="alpaca_paper",
            lifecycle_state="position_reconciled",
            execution_symbol="BTC/USD",
            execution_venue="alpaca_paper",
            instrument_type=InstrumentType.crypto,
            side="buy",
            order_type="limit",
            currency="USD",
            requested_qty=Decimal("0.002"),
            filled_qty=Decimal("0.002"),
            order_status="filled",
            broker_order_id="native-source-order",
            submitted_at=_NOW - timedelta(minutes=2),
            reconciled_at=_NOW - timedelta(minutes=1),
            confirm_flag=True,
        )
    )
    await db_session.commit()
    db_session.expire_all()


async def test_source_bound_automated_sell_reuses_coordinator_safety(db_session):
    await _seed_source_buy(db_session, client_order_id="source-buy-1")
    canonical = _sell_canonical()
    packet = _source_bound_packet(canonical, source_client_order_id="source-buy-1")
    broker = _Broker()
    coordinator = AlpacaPaperSubmitCoordinator(
        AlpacaPaperLedgerService(db_session),
        lambda: broker,
        now_fn=lambda: _NOW,
    )

    outcome = await coordinator.submit(packet, submit_canonical=canonical)

    assert outcome.status == "submitted"
    assert outcome.reason_code is None
    assert len(broker.submit_calls) == 1


async def test_legacy_source_less_automated_sell_stays_disabled(db_session):
    canonical = _sell_canonical()
    packet = _source_bound_packet(canonical, source_client_order_id=None)
    broker = _Broker()
    coordinator = AlpacaPaperSubmitCoordinator(
        AlpacaPaperLedgerService(db_session),
        lambda: broker,
        now_fn=lambda: _NOW,
    )

    outcome = await coordinator.submit(packet, submit_canonical=canonical)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "automated_sell_disabled"
    assert broker.submit_calls == []


async def test_verified_application_persists_preview_then_submits_through_coordinator(
    db_session,
):
    from app.core.db import AsyncSessionLocal
    from app.services.alpaca_paper_order_application import (
        AlpacaPaperOrderApplication,
        AlpacaPaperOrderSpec,
        AlpacaVerifiedDecision,
    )

    broker = _Broker()
    application = AlpacaPaperOrderApplication(
        session_factory=AsyncSessionLocal,
        broker_factory=lambda: broker,
        now_fn=lambda: _NOW,
    )
    decision = AlpacaVerifiedDecision(
        order=AlpacaPaperOrderSpec(
            symbol="BTC/USD",
            side="buy",
            order_type="limit",
            qty=Decimal("0.0005"),
            notional=None,
            time_in_force="gtc",
            limit_price=Decimal("50000"),
            asset_class="crypto",
        ),
        decision_id="rob845-decision-buy",
        signal_symbol="BTCUSDT",
        signal_venue="binance_public_spot",
        snapshot_id="rob845-snapshot-buy",
        snapshot_hash="sha256:snapshot-buy",
        snapshot_as_of=_NOW,
        snapshot_source="binance_public_spot",
        reference_price=Decimal("50000"),
        source_buy_client_order_id=None,
        decision_identity_hash="d" * 64,
    )

    preview = await application.preview(decision)
    submitted = await application.submit(decision)

    assert preview.status == "previewed"
    assert preview.native_client_order_id
    assert submitted.status == "submitted"
    assert submitted.native_client_order_id == preview.native_client_order_id
    assert len(broker.submit_calls) == 1


async def test_verified_application_rejects_sell_over_source_before_broker(db_session):
    from app.core.db import AsyncSessionLocal
    from app.services.alpaca_paper_order_application import (
        AlpacaPaperOrderApplication,
        AlpacaPaperOrderSpec,
        AlpacaVerifiedDecision,
    )

    await _seed_source_buy(db_session, client_order_id="source-buy-1")
    broker = _Broker()
    application = AlpacaPaperOrderApplication(
        session_factory=AsyncSessionLocal,
        broker_factory=lambda: broker,
        now_fn=lambda: _NOW,
    )
    decision = AlpacaVerifiedDecision(
        order=AlpacaPaperOrderSpec(
            symbol="BTC/USD",
            side="sell",
            order_type="limit",
            qty=Decimal("0.003"),
            notional=None,
            time_in_force="gtc",
            limit_price=Decimal("10000"),
            asset_class="crypto",
        ),
        decision_id="rob845-decision-over-source",
        signal_symbol="BTCUSDT",
        signal_venue="binance_public_spot",
        snapshot_id="rob845-snapshot-over-source",
        snapshot_hash="sha256:snapshot-over-source",
        snapshot_as_of=_NOW,
        snapshot_source="binance_public_spot",
        reference_price=Decimal("10000"),
        source_buy_client_order_id="source-buy-1",
        decision_identity_hash="e" * 64,
    )

    outcome = await application.submit(decision)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "qty_exceeds_source"
    assert broker.submit_calls == []
