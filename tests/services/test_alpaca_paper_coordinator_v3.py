"""ROB-842 3rd-round coordinator tests: live sell position (B3) + terminal/uncertain broker outcomes (B4)."""

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
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
from app.services.alpaca_paper_submit_service import (
    AlpacaPaperSubmitCoordinator,
    build_canonical_payload,
    canonical_hash,
    derive_automated_key,
)
from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError
from app.services.brokers.alpaca.schemas import Order, Position

pytestmark = [pytest.mark.asyncio]

_NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
_FUTURE = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
_CORR = "rob842-v3"


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
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


class V3Broker:
    def __init__(
        self,
        *,
        position: Any = None,
        submit_error: Exception | None = None,
        lookup_order: Order | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.submit_calls: list[Any] = []
        self.position = position
        self.submit_error = submit_error
        self.lookup_order = lookup_order
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
        return self.lookup_order


def _canonical(side: str, *, qty=None, notional=None, limit="50000") -> dict[str, Any]:
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


def _packet(canonical, corr, *, origin="automated", **overrides):
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
        "expires_at": _FUTURE,
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


def _coord(db_session, broker, **kw):
    return AlpacaPaperSubmitCoordinator(
        AlpacaPaperLedgerService(db_session), lambda: broker, now_fn=lambda: _NOW, **kw
    )


async def _seed_reconciled_buy(db_session, corr, *, filled_qty="5"):
    ledger = AlpacaPaperLedgerService(db_session)
    buy_coid = f"{corr}-buysrc"
    await ledger.claim_submit(
        client_order_id=buy_coid,
        lifecycle_correlation_id=corr,
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
        order_type="limit",
        time_in_force="gtc",
        requested_qty=Decimal(filled_qty),
        requested_price=Decimal("50000"),
    )
    await ledger.record_submit(
        buy_coid,
        {
            "id": "buy-1",
            "status": "filled",
            "filled_qty": filled_qty,
            "filled_avg_price": "50000",
        },
    )
    db_session.expire_all()


def _sell_packet(corr, *, qty="1", max_qty="5"):
    # Sell path is manual-only now (automated sell is disabled at the boundary).
    canonical = _canonical("sell", qty=qty)
    return canonical, _packet(
        canonical,
        corr,
        origin="manual",
        side="sell",
        max_notional=None,
        max_qty=Decimal(max_qty),
        qty_source="manual_operator",
    )


# ---------------------------------------------------------------------------
# B3 — sell must be backed by a FRESH current position
# ---------------------------------------------------------------------------
async def test_sell_blocked_when_current_position_zero_despite_past_fill(db_session):
    corr = f"{_CORR}-sellzero"
    await _seed_reconciled_buy(
        db_session, corr, filled_qty="5"
    )  # past fill = provenance
    canonical, packet = _sell_packet(corr, qty="1")
    broker = V3Broker(
        position=Position(
            asset_id="btc",
            symbol="BTCUSD",
            qty=Decimal("0"),
            qty_available=None,
            avg_entry_price=Decimal("50000"),
            side="long",
        )
    )
    coord = _coord(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "position_available_unavailable"
    assert broker.submit_calls == []  # never POSTed


async def test_sell_blocked_when_qty_exceeds_current_position(db_session):
    corr = f"{_CORR}-sellover"
    await _seed_reconciled_buy(db_session, corr, filled_qty="5")
    canonical, packet = _sell_packet(corr, qty="3")
    broker = V3Broker(
        position=SimpleNamespace(
            symbol="BTCUSD", qty=Decimal("1"), qty_available=Decimal("1")
        )
    )
    coord = _coord(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "qty_exceeds_available"
    assert broker.submit_calls == []


async def test_sell_blocked_when_position_malformed(db_session):
    corr = f"{_CORR}-sellmalf"
    await _seed_reconciled_buy(db_session, corr, filled_qty="5")
    canonical, packet = _sell_packet(corr, qty="1")
    broker = V3Broker(position=SimpleNamespace(symbol="BTCUSD", qty=None))
    coord = _coord(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "position_malformed"
    assert broker.submit_calls == []


async def test_sell_blocked_on_symbol_mismatch(db_session):
    corr = f"{_CORR}-sellsym"
    await _seed_reconciled_buy(db_session, corr, filled_qty="5")
    canonical, packet = _sell_packet(corr, qty="1")
    broker = V3Broker(position=SimpleNamespace(symbol="ETHUSD", qty=Decimal("5")))
    coord = _coord(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "position_symbol_mismatch"
    assert broker.submit_calls == []


async def test_sell_succeeds_within_current_position(db_session):
    corr = f"{_CORR}-sellok"
    await _seed_reconciled_buy(db_session, corr, filled_qty="5")
    canonical, packet = _sell_packet(corr, qty="1")
    broker = V3Broker(
        position=SimpleNamespace(
            symbol="BTCUSD", qty=Decimal("2"), qty_available=Decimal("2")
        )
    )
    coord = _coord(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "submitted"
    assert len(broker.submit_calls) == 1


# ---------------------------------------------------------------------------
# B4 — deterministic broker failure is terminal + replayed; uncertain reconciles
# ---------------------------------------------------------------------------
async def test_http_422_is_terminal_and_replayed_no_second_post(db_session):
    corr = f"{_CORR}-422"
    canonical = _canonical("buy", notional="10")
    packet = _packet(canonical, corr)
    broker = V3Broker(submit_error=AlpacaPaperRequestError("bad", status_code=422))
    coord = _coord(db_session, broker)

    first = await coord.submit(packet, submit_canonical=canonical)
    second = await coord.submit(packet, submit_canonical=canonical)

    assert first.status == "failed"
    assert first.reason_code == "broker_rejected"
    assert second.status == "failed"
    assert second.reason_code == "broker_rejected_replayed"
    assert second.broker_called is False
    assert len(broker.submit_calls) == 1  # exactly one POST total


async def test_parallel_422_same_failure_single_post(db_session):
    from app.core.db import AsyncSessionLocal

    corr = f"{_CORR}-422par"
    canonical = _canonical("buy", notional="10")
    barrier = asyncio.Barrier(2)
    shared = V3Broker(
        submit_error=AlpacaPaperRequestError("bad", status_code=422), delay_s=0.02
    )

    async def _run():
        async with AsyncSessionLocal() as s:
            coord = AlpacaPaperSubmitCoordinator(
                AlpacaPaperLedgerService(s),
                lambda: shared,
                now_fn=lambda: _NOW,
                inflight_max_polls=20,
                inflight_poll_interval_s=0.02,
            )
            await barrier.wait()
            return await coord.submit(
                _packet(canonical, corr), submit_canonical=canonical
            )

    outcomes = await asyncio.gather(_run(), _run())
    assert len(shared.submit_calls) == 1
    assert sum(1 for o in outcomes if o.broker_called) == 1
    assert all(o.status in {"failed", "idempotency_in_progress"} for o in outcomes)
    assert any(o.status == "failed" for o in outcomes)


async def test_uncertain_500_with_broker_lookup_recovers_no_repost(db_session):
    corr = f"{_CORR}-uncrec"
    canonical = _canonical("buy", notional="10")
    packet = _packet(canonical, corr)
    recovered = Order(
        id="paper-recovered",
        client_order_id=packet.client_order_id,
        symbol="BTC/USD",
        filled_qty=Decimal("0"),
        side="buy",
        type="limit",
        time_in_force="gtc",
        status="accepted",
    )
    broker = V3Broker(
        submit_error=AlpacaPaperRequestError("boom", status_code=500),
        lookup_order=recovered,
    )
    coord = _coord(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "recovered"
    assert len(broker.submit_calls) == 1  # the failed attempt; no re-POST


async def test_uncertain_500_without_lookup_stays_in_flight_no_repost(db_session):
    corr = f"{_CORR}-uncflight"
    canonical = _canonical("buy", notional="10")
    packet = _packet(canonical, corr)
    broker = V3Broker(
        submit_error=AlpacaPaperRequestError("boom", status_code=500), lookup_order=None
    )
    coord = _coord(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "idempotency_in_progress"
    assert len(broker.submit_calls) == 1
    # retry does not re-POST either
    retry = await coord.submit(packet, submit_canonical=canonical)
    assert retry.status == "idempotency_in_progress"
    assert len(broker.submit_calls) == 1


async def test_terminal_persistence_failure_still_no_duplicate_post(
    db_session, monkeypatch
):
    corr = f"{_CORR}-persistfail"
    canonical = _canonical("buy", notional="10")
    packet = _packet(canonical, corr)
    broker = V3Broker(submit_error=AlpacaPaperRequestError("bad", status_code=422))
    ledger = AlpacaPaperLedgerService(db_session)

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ledger, "record_submit_failure", _boom)
    coord = AlpacaPaperSubmitCoordinator(ledger, lambda: broker, now_fn=lambda: _NOW)

    first = await coord.submit(packet, submit_canonical=canonical)
    assert first.status == "failed"
    assert first.reason_code == "broker_rejected_unpersisted"
    # retry must not POST again (claim row still guards)
    second = await coord.submit(packet, submit_canonical=canonical)
    assert len(broker.submit_calls) == 1
    assert second.broker_called is False


# ---------------------------------------------------------------------------
# ROB-935: Submit failure error body capture & sanitize/truncate tests
# ---------------------------------------------------------------------------
async def test_alpaca_submit_failure_captures_error_body(db_session):
    corr = f"{_CORR}-errorbody"
    canonical = _canonical("buy", notional="10")
    packet = _packet(canonical, corr)

    error_msg = 'HTTP 403: {"message":"crypto trading not enabled"}'
    broker = V3Broker(submit_error=AlpacaPaperRequestError(error_msg, status_code=403))
    coord = _coord(db_session, broker)

    first = await coord.submit(packet, submit_canonical=canonical)
    assert first.status == "failed"
    assert first.reason_code == "broker_rejected"

    ledger = AlpacaPaperLedgerService(db_session)
    row = await ledger.get_execution_by_client_order_id(packet.client_order_id)
    assert row is not None
    assert (
        row.error_summary
        == 'broker_rejected: HTTP 403 \u2014 {"message":"crypto trading not enabled"}'
    )


async def test_alpaca_submit_failure_sanitizes_sensitive_tokens(db_session):
    corr = f"{_CORR}-sanitize"
    canonical = _canonical("buy", notional="10")
    packet = _packet(canonical, corr)

    sensitive_token = "abcdefghijklmnopqrstuvwxyz1234567890abcd"
    uuid_str = "123e4567-e89b-12d3-a456-426614174000"
    error_msg = (
        f"HTTP 403: secret: {sensitive_token} and raw_token: {sensitive_token} "
        f"and normal_word: cryptotradingnotenabled and uuid: {uuid_str}"
    )
    broker = V3Broker(submit_error=AlpacaPaperRequestError(error_msg, status_code=403))
    coord = _coord(db_session, broker)

    await coord.submit(packet, submit_canonical=canonical)

    ledger = AlpacaPaperLedgerService(db_session)
    row = await ledger.get_execution_by_client_order_id(packet.client_order_id)
    assert row is not None
    assert "secret: [REDACTED]" in row.error_summary
    assert "raw_token: [MASKED_TOKEN]" in row.error_summary
    assert "normal_word: cryptotradingnotenabled" in row.error_summary
    assert f"uuid: {uuid_str}" in row.error_summary
    assert sensitive_token not in row.error_summary


async def test_extract_and_sanitize_error_body_truncates_and_masks():
    from app.services.alpaca_paper_submit_service import (
        _extract_and_sanitize_error_body,
    )

    long_text = "word " * 400
    exc = AlpacaPaperRequestError(f"HTTP 403: {long_text}", status_code=403)
    result = _extract_and_sanitize_error_body(exc)
    assert len(result) == 500

    exc_mask = AlpacaPaperRequestError(
        "HTTP 403: token12345678901234567890", status_code=403
    )
    result_mask = _extract_and_sanitize_error_body(exc_mask)
    assert result_mask == "[MASKED_TOKEN]"

    # pure alphabetic long word must not be masked
    exc_clear = AlpacaPaperRequestError(
        "HTTP 403: cryptotradingnotenabled", status_code=403
    )
    result_clear = _extract_and_sanitize_error_body(exc_clear)
    assert result_clear == "cryptotradingnotenabled"


async def test_extract_and_sanitize_error_body_backtracking_prevention():
    import time

    from app.services.alpaca_paper_submit_service import (
        _extract_and_sanitize_error_body,
    )

    text = "Authorization: Bearer " + ("A1" * 512)
    exc = AlpacaPaperRequestError(text, status_code=403)

    t0 = time.perf_counter()
    _ = _extract_and_sanitize_error_body(exc)
    t1 = time.perf_counter()

    duration = t1 - t0
    assert duration < 0.1, (
        f"Authorization regex backtracked catastrophically, took {duration:.4f}s"
    )
