"""ROB-286 — Scalper runner lifecycle integration test.

Matrix row T28.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
import pytest_asyncio

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.testnet.execution_client import (
    BinanceTestnetExecutionClient,
)
from app.services.brokers.binance.testnet.ledger.service import (
    BinanceTestnetLedgerService,
)
from app.services.scalping.config import ScalperConfig
from app.services.scalping.decision import MarketSnapshot
from app.services.scalping.runner import ScalperRunner


@pytest_asyncio.fixture
async def instrument(db_session) -> CryptoInstrument:
    from sqlalchemy import select

    existing = await db_session.scalar(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "spot",
            CryptoInstrument.venue_symbol == "BTCUSDT",
        )
    )
    if existing is not None:
        return existing
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    await db_session.refresh(inst)
    return inst


@pytest.fixture
def execution_client(monkeypatch) -> BinanceTestnetExecutionClient:
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    return BinanceTestnetExecutionClient.from_env()


def _instrument_id_factory(
    instrument_id: int,
) -> Callable[[str], Awaitable[int]]:
    async def _get(symbol: str) -> int:
        return instrument_id

    return _get


def _snapshot_factory(
    snapshot: MarketSnapshot,
) -> Callable[[str], Awaitable[MarketSnapshot]]:
    async def _get(symbol: str) -> MarketSnapshot:
        return snapshot

    return _get


@pytest.mark.asyncio
async def test_lifecycle_happy_path_dry_run(
    db_session,
    instrument: CryptoInstrument,
    execution_client: BinanceTestnetExecutionClient,
) -> None:
    """T28 — End-to-end dry-run lifecycle.

    In dry-run mode the runner exercises planned → previewed → validated
    but never reaches ``submitted`` (no HTTP). The ledger trail proves
    the orchestration is plumbed end-to-end without any broker hit.
    """
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    snap = MarketSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("50000"),
        rsi_5m=20.0,  # oversold
        ema_20_5m=Decimal("49600"),
        ema_50_5m=Decimal("49000"),
        instrument_health="healthy",
    )
    runner = ScalperRunner(
        execution_client=execution_client,
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(snap),
        dry_run=True,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "entry"
    assert tick.submitted is False  # dry-run — never submitted
    assert tick.dry_run is True
    # Ledger trail: a planned row in 'validated' state (we record_plan +
    # record_preview + record_validation pre-submit).
    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    assert len(rows) == 1
    assert rows[0].lifecycle_state == "validated"
    assert rows[0].tp_price is not None
    assert rows[0].sl_price is not None
    assert rows[0].notional_usdt == Decimal("10")
    await execution_client.aclose()


@pytest.mark.asyncio
async def test_tick_hold_when_no_signal(
    db_session,
    instrument: CryptoInstrument,
    execution_client: BinanceTestnetExecutionClient,
) -> None:
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    snap = MarketSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("50000"),
        rsi_5m=50.0,  # neutral
        ema_20_5m=Decimal("50000"),
        ema_50_5m=Decimal("50000"),
        instrument_health="healthy",
    )
    runner = ScalperRunner(
        execution_client=execution_client,
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(snap),
        dry_run=True,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "hold"
    assert tick.submitted is False
    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    assert rows == []
    await execution_client.aclose()


# --------------------------------------------------------------------------
# ROB-289 — Paired TP/SL broker placement tests (TT7-TT13).
#
# These tests use a fake ExecutionClient subclass instead of httpx_mock
# so we can inject placement results / failures deterministically. The
# transport-level signed-host invariants are covered by TT2/TT4/TT5.
# --------------------------------------------------------------------------


class _FakeExecutionClient:
    """Test double standing in for ``BinanceTestnetExecutionClient``.

    Records every call site for assertion; defaults to returning a
    ``StopOrderResult`` for placements and a ``CancelResult`` for cancels.
    Tests override ``place_stop_limit_order`` /
    ``place_stop_market_order`` / ``cancel_order`` / ``submit_order``
    directly to inject failures.
    """

    def __init__(self) -> None:
        self.placement_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.submit_calls: list[dict] = []
        self._counter = 0
        # Default values; tests overwrite as needed.
        self.entry_fill_status: str = "FILLED"
        self.entry_fill_price = Decimal("50000")

    def _next_broker_id(self) -> str:
        self._counter += 1
        return f"broker-{self._counter}"

    def _new_client_order_id(self) -> str:
        self._counter += 1
        return f"cid-{self._counter}"

    async def submit_order(self, **kwargs):
        from app.services.brokers.binance.testnet.dto import OrderSubmitResult

        self.submit_calls.append(kwargs)
        return OrderSubmitResult(
            client_order_id=kwargs["client_order_id"],
            broker_order_id=self._next_broker_id(),
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            order_type=kwargs["order_type"],
            quantity=kwargs["quantity"],
            price=kwargs.get("price"),
            status=self.entry_fill_status,
            transact_time_ms=1700000000000,
            raw_response={},
        )

    async def place_stop_limit_order(self, **kwargs):
        from app.services.brokers.binance.testnet.dto import StopOrderResult

        self.placement_calls.append({"leg": "tp", **kwargs})
        return StopOrderResult(
            broker_order_id=self._next_broker_id(),
            client_order_id=kwargs["client_order_id"],
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            order_type="STOP_LOSS_LIMIT",
            stop_price=kwargs["stop_price"],
            limit_price=kwargs["limit_price"],
            status="NEW",
            transact_time_ms=1700000000001,
            raw_response={},
        )

    async def place_stop_market_order(self, **kwargs):
        from app.services.brokers.binance.testnet.dto import StopOrderResult

        self.placement_calls.append({"leg": "sl", **kwargs})
        return StopOrderResult(
            broker_order_id=self._next_broker_id(),
            client_order_id=kwargs["client_order_id"],
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            order_type="STOP_LOSS",
            stop_price=kwargs["stop_price"],
            limit_price=None,
            status="NEW",
            transact_time_ms=1700000000002,
            raw_response={},
        )

    async def cancel_order(self, **kwargs):
        from app.services.brokers.binance.testnet.dto import CancelResult

        self.cancel_calls.append(kwargs)
        return CancelResult(
            client_order_id=kwargs["client_order_id"],
            broker_order_id=self._next_broker_id(),
            symbol=kwargs["symbol"],
            status="CANCELED",
            raw_response={},
        )

    async def aclose(self) -> None:
        return


def _make_runner_with_fake(
    *,
    db_session,
    instrument_id: int,
    snapshot: MarketSnapshot,
    fake_client: _FakeExecutionClient,
    dry_run: bool = False,
) -> ScalperRunner:
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    return ScalperRunner(
        execution_client=fake_client,  # type: ignore[arg-type]
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument_id),
        market_snapshot_for_symbol=_snapshot_factory(snapshot),
        dry_run=dry_run,
    )


def _entry_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("50000"),
        rsi_5m=20.0,
        ema_20_5m=Decimal("49600"),
        ema_50_5m=Decimal("49000"),
        instrument_health="healthy",
    )


@pytest.mark.asyncio
async def test_runner_places_tp_sl_pair_after_entry_fill(
    db_session,
    instrument: CryptoInstrument,
) -> None:
    """TT7 — Entry submitted → filled → both TP and SL ``tp_sl_armed``.

    Full happy path: the runner records the entry, observes the broker
    return ``status=FILLED`` in the submit response, transitions the
    entry row to ``filled``, then places paired TP (STOP_LOSS_LIMIT)
    and SL (STOP_LOSS) legs SEQUENTIALLY (not via gather), recording
    each as a separate ledger row linked by ``parent_client_order_id``.
    """
    fake = _FakeExecutionClient()
    runner = _make_runner_with_fake(
        db_session=db_session,
        instrument_id=instrument.id,
        snapshot=_entry_snapshot(),
        fake_client=fake,
        dry_run=False,  # confirm path
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "entry"
    assert tick.submitted is True
    # Exactly one submit (entry); exactly two stop placements (TP, SL).
    assert len(fake.submit_calls) == 1
    assert len(fake.placement_calls) == 2
    # Sequential order: TP first, SL second.
    assert fake.placement_calls[0]["leg"] == "tp"
    assert fake.placement_calls[1]["leg"] == "sl"
    # Ledger rows: entry (filled) + 2 paired (tp_sl_armed).
    ledger = runner.ledger_service
    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    by_state: dict[str, list] = {}
    for r in rows:
        by_state.setdefault(r.lifecycle_state, []).append(r)
    assert "filled" in by_state and len(by_state["filled"]) == 1
    assert "tp_sl_armed" in by_state and len(by_state["tp_sl_armed"]) == 2
    entry_cid = by_state["filled"][0].client_order_id
    # Both paired legs reference the entry CID as parent.
    for arm in by_state["tp_sl_armed"]:
        assert arm.parent_client_order_id == entry_cid
        # Reviewer focus #4 — sibling lookup is by parent_client_order_id;
        # never by the TP/SL CIDs themselves. CID is suffixed with -tp / -sl.
        assert arm.client_order_id != entry_cid
        assert arm.client_order_id in (f"{entry_cid}-tp", f"{entry_cid}-sl")
        assert arm.broker_order_id is not None
        # Metadata records leg + stop_price for the audit trail.
        assert arm.extra_metadata is not None
        assert arm.extra_metadata.get("tp_or_sl") in {"tp", "sl"}


async def _seed_filled_entry_with_paired_legs(
    *,
    db_session,
    instrument_id: int,
) -> dict[str, str]:
    """Seed an entry + TP + SL ledger trail in tp_sl_armed state.

    Returns the CIDs as ``{"entry": e, "tp": e-tp, "sl": e-sl}``.
    """
    ledger = BinanceTestnetLedgerService(session=db_session)
    entry_cid = "entry-xyz"
    await ledger.record_plan(
        instrument_id=instrument_id,
        client_order_id=entry_cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
        tp_price=Decimal("50500"),
        sl_price=Decimal("49500"),
    )
    await ledger.record_preview(client_order_id=entry_cid)
    await ledger.record_validation(client_order_id=entry_cid)
    await ledger.record_submit(
        client_order_id=entry_cid, broker_order_id="entry-broker"
    )
    await ledger.record_fill(client_order_id=entry_cid)

    tp_cid = f"{entry_cid}-tp"
    sl_cid = f"{entry_cid}-sl"
    for cid, price_kw in (
        (tp_cid, {"price": Decimal("50500"), "tp_price": Decimal("50500")}),
        (sl_cid, {"sl_price": Decimal("49500")}),
    ):
        await ledger.record_plan(
            instrument_id=instrument_id,
            client_order_id=cid,
            side="SELL",
            order_type="LIMIT" if cid == tp_cid else "MARKET",
            qty=Decimal("0.001"),
            parent_client_order_id=entry_cid,
            **price_kw,
        )
        await ledger.record_preview(client_order_id=cid)
        await ledger.record_validation(client_order_id=cid)
        await ledger.record_submit(client_order_id=cid, broker_order_id=f"broker-{cid}")
        await ledger.record_fill(client_order_id=cid)
        await ledger.record_tp_sl_armed(
            client_order_id=cid,
            broker_order_id=f"broker-{cid}",
            tp_or_sl="tp" if cid == tp_cid else "sl",
        )
    return {"entry": entry_cid, "tp": tp_cid, "sl": sl_cid}


@pytest.mark.asyncio
async def test_runner_cancels_opposite_leg_when_tp_triggers(
    db_session,
    instrument: CryptoInstrument,
) -> None:
    """TT8 — TP triggers (last_price >= tp_price) → SL cancelled.

    The decision function returns Exit(take_profit), the runner
    transitions the TP leg to ``tp_sl_triggered`` and cancels the SL
    leg at the broker, recording ``cancel_reason=opposite_leg_triggered``.
    """
    cids = await _seed_filled_entry_with_paired_legs(
        db_session=db_session, instrument_id=instrument.id
    )
    fake = _FakeExecutionClient()
    # Snapshot above TP price triggers Exit(take_profit).
    snap = MarketSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("50600"),
        rsi_5m=50.0,
        ema_20_5m=Decimal("50000"),
        ema_50_5m=Decimal("50000"),
        instrument_health="healthy",
    )
    runner = _make_runner_with_fake(
        db_session=db_session,
        instrument_id=instrument.id,
        snapshot=snap,
        fake_client=fake,
        dry_run=False,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "exit"
    # Exactly one cancel (the SL leg, opposite of TP that triggered).
    assert len(fake.cancel_calls) == 1
    assert fake.cancel_calls[0]["client_order_id"] == cids["sl"]
    # Ledger: TP → tp_sl_triggered; SL → cancelled with reason.
    ledger = runner.ledger_service
    tp_row = await ledger.get_by_client_order_id(cids["tp"])
    sl_row = await ledger.get_by_client_order_id(cids["sl"])
    assert tp_row is not None and tp_row.lifecycle_state == "tp_sl_triggered"
    assert sl_row is not None and sl_row.lifecycle_state == "cancelled"
    assert sl_row.extra_metadata is not None
    assert sl_row.extra_metadata.get("cancel_reason") == "opposite_leg_triggered"


@pytest.mark.asyncio
async def test_runner_cancels_opposite_leg_when_sl_triggers(
    db_session,
    instrument: CryptoInstrument,
) -> None:
    """TT9 — SL triggers → TP cancelled (mirror of TT8)."""
    cids = await _seed_filled_entry_with_paired_legs(
        db_session=db_session, instrument_id=instrument.id
    )
    fake = _FakeExecutionClient()
    snap = MarketSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("49400"),  # below SL price
        rsi_5m=50.0,
        ema_20_5m=Decimal("50000"),
        ema_50_5m=Decimal("50000"),
        instrument_health="healthy",
    )
    runner = _make_runner_with_fake(
        db_session=db_session,
        instrument_id=instrument.id,
        snapshot=snap,
        fake_client=fake,
        dry_run=False,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "exit"
    assert len(fake.cancel_calls) == 1
    assert fake.cancel_calls[0]["client_order_id"] == cids["tp"]
    ledger = runner.ledger_service
    sl_row = await ledger.get_by_client_order_id(cids["sl"])
    tp_row = await ledger.get_by_client_order_id(cids["tp"])
    assert sl_row is not None and sl_row.lifecycle_state == "tp_sl_triggered"
    assert tp_row is not None and tp_row.lifecycle_state == "cancelled"
    assert tp_row.extra_metadata is not None
    assert tp_row.extra_metadata.get("cancel_reason") == "opposite_leg_triggered"


@pytest.mark.asyncio
async def test_first_leg_success_second_leg_reject_cancels_first(
    db_session,
    instrument: CryptoInstrument,
) -> None:
    """TT10 — §3.1 most dangerous path.

    First leg (TP) succeeds at the broker; second leg (SL) returns 4xx.
    The runner MUST immediately cancel the first leg synchronously
    before returning, and record anomaly on the entry row so the
    operator is alerted.
    """
    import httpx

    fake = _FakeExecutionClient()

    async def _sl_rejects(**kwargs):
        request = httpx.Request("POST", "https://testnet.binance.vision/api/v3/order")
        response = httpx.Response(
            400, json={"code": -2010, "msg": "rejected"}, request=request
        )
        raise httpx.HTTPStatusError("rejected", request=request, response=response)

    fake.place_stop_market_order = _sl_rejects  # type: ignore[assignment]

    runner = _make_runner_with_fake(
        db_session=db_session,
        instrument_id=instrument.id,
        snapshot=_entry_snapshot(),
        fake_client=fake,
        dry_run=False,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "entry"
    # TP placed once at the broker; cancel called once for the first
    # leg (the §3.1 emergency cancel).
    assert len(fake.placement_calls) == 1
    assert fake.placement_calls[0]["leg"] == "tp"
    assert len(fake.cancel_calls) == 1
    # The cancel target is the TP CID we placed.
    tp_cid_pattern = fake.placement_calls[0]["client_order_id"]
    assert fake.cancel_calls[0]["client_order_id"] == tp_cid_pattern
    # Ledger: entry row in anomaly; TP row cancelled with fallback
    # reason; SL row in anomaly (planned → anomaly is legal).
    ledger = runner.ledger_service
    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    by_state: dict[str, list] = {}
    for r in rows:
        by_state.setdefault(r.lifecycle_state, []).append(r)
    assert "anomaly" in by_state
    # At least two anomaly rows: entry + rejected SL leg.
    assert len(by_state["anomaly"]) >= 2
    # TP leg is cancelled with the fallback_after_broker_reject reason.
    cancelled_tp = [
        r for r in by_state.get("cancelled", []) if r.client_order_id == tp_cid_pattern
    ]
    assert len(cancelled_tp) == 1
    assert cancelled_tp[0].extra_metadata is not None
    assert (
        cancelled_tp[0].extra_metadata.get("cancel_reason")
        == "fallback_after_broker_reject"
    )


@pytest.mark.asyncio
async def test_first_leg_reject_falls_back_to_cancel_and_close(
    db_session,
    instrument: CryptoInstrument,
) -> None:
    """TT11 — §3.2 first-leg-reject fallback.

    The TP placement returns 4xx, so the SL placement is SKIPPED entirely
    (the second-leg call never happens). The entry row is moved to
    ``anomaly`` with reason ``tp_sl_placement_rejected``.
    """
    import httpx

    fake = _FakeExecutionClient()

    async def _tp_rejects(**kwargs):
        request = httpx.Request("POST", "https://testnet.binance.vision/api/v3/order")
        response = httpx.Response(
            400, json={"code": -1100, "msg": "rejected"}, request=request
        )
        raise httpx.HTTPStatusError("rejected", request=request, response=response)

    fake.place_stop_limit_order = _tp_rejects  # type: ignore[assignment]

    runner = _make_runner_with_fake(
        db_session=db_session,
        instrument_id=instrument.id,
        snapshot=_entry_snapshot(),
        fake_client=fake,
        dry_run=False,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "entry"
    # SL placement never attempted (no retry on the rejected leg per §3.2).
    assert fake.placement_calls == []
    # No cancel call (first leg never reached the broker).
    assert fake.cancel_calls == []
    # Entry row in anomaly.
    ledger = runner.ledger_service
    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    anomaly_rows = [r for r in rows if r.lifecycle_state == "anomaly"]
    # Entry + TP leg both transitioned to anomaly (TP from planned→anomaly).
    assert len(anomaly_rows) >= 2
    # At least one anomaly carries the rejected reason.
    rejected = [
        r for r in anomaly_rows if r.anomaly_reason == "tp_sl_placement_rejected"
    ]
    assert len(rejected) >= 1


@pytest.mark.asyncio
async def test_sibling_cancel_failure_records_anomaly(
    db_session,
    instrument: CryptoInstrument,
) -> None:
    """TT12 — §3.3 sibling cancel failure records anomaly.

    Both legs armed; TP triggers; the sibling SL cancel call raises
    at the broker. The runner records ``opposite_leg_cancel_failed``
    on the SL row; do NOT auto-retry.
    """
    cids = await _seed_filled_entry_with_paired_legs(
        db_session=db_session, instrument_id=instrument.id
    )
    fake = _FakeExecutionClient()

    async def _cancel_fails(**kwargs):
        raise RuntimeError("broker cancel failed at testnet")

    fake.cancel_order = _cancel_fails  # type: ignore[assignment]

    snap = MarketSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("50600"),  # >= tp_price
        rsi_5m=50.0,
        ema_20_5m=Decimal("50000"),
        ema_50_5m=Decimal("50000"),
        instrument_health="healthy",
    )
    runner = _make_runner_with_fake(
        db_session=db_session,
        instrument_id=instrument.id,
        snapshot=snap,
        fake_client=fake,
        dry_run=False,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "exit"
    ledger = runner.ledger_service
    sl_row = await ledger.get_by_client_order_id(cids["sl"])
    assert sl_row is not None
    assert sl_row.lifecycle_state == "anomaly"
    assert sl_row.anomaly_reason == "opposite_leg_cancel_failed"


@pytest.mark.asyncio
async def test_placement_network_timeout_records_unknown_state(
    db_session,
    instrument: CryptoInstrument,
) -> None:
    """TT13 — §3.5 partial network failure during placement.

    Treat as ``unknown state`` — record anomaly with reason
    ``tp_sl_placement_unknown`` on the in-flight leg so reconciliation
    walks ``open_orders`` + ``recent_fills`` and resolves on the next
    runner startup.
    """
    import httpx

    fake = _FakeExecutionClient()

    async def _tp_times_out(**kwargs):
        raise httpx.TimeoutException("connection timed out")

    fake.place_stop_limit_order = _tp_times_out  # type: ignore[assignment]

    runner = _make_runner_with_fake(
        db_session=db_session,
        instrument_id=instrument.id,
        snapshot=_entry_snapshot(),
        fake_client=fake,
        dry_run=False,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "entry"
    ledger = runner.ledger_service
    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    anomaly_rows = [r for r in rows if r.lifecycle_state == "anomaly"]
    unknown = [r for r in anomaly_rows if r.anomaly_reason == "tp_sl_placement_unknown"]
    assert len(unknown) >= 1, (
        f"Expected at least one tp_sl_placement_unknown anomaly; got {anomaly_rows}"
    )


@pytest.mark.asyncio
async def test_tick_rejects_symbol_outside_mvp_set(
    db_session,
    instrument: CryptoInstrument,
    execution_client: BinanceTestnetExecutionClient,
) -> None:
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    snap = MarketSnapshot(
        symbol="DOGEUSDT",
        last_price=Decimal("0.1"),
        rsi_5m=50.0,
        ema_20_5m=Decimal("0.1"),
        ema_50_5m=Decimal("0.1"),
        instrument_health="healthy",
    )
    runner = ScalperRunner(
        execution_client=execution_client,
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(snap),
        dry_run=True,
    )
    with pytest.raises(ValueError, match="not in the MVP locked set"):
        await runner.tick_once(symbol="DOGEUSDT")
    await execution_client.aclose()
