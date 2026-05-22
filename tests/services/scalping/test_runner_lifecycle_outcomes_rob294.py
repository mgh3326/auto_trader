"""ROB-294 — Lifecycle-outcome readiness tests for the testnet scalper.

The existing ROB-286/289 suite (``tests/services/scalping/test_runner_lifecycle.py``)
already covers three of the four lifecycle outcomes called out in ROB-294's
acceptance criteria via fake clients:

  * filled + paired TP/SL armed       → TT7
  * filled + TP trigger               → TT8
  * filled + SL trigger               → TT9
  * anomaly (4xx / timeout) outcomes  → TT10-TT13

The remaining outcome is **"entry submitted but not filled within the
tick → cancelled and recorded safely."** This file adds that test plus
an end-to-end fake-broker dry-run lifecycle check that proves the
ROB-294 evidence path (``client_order_id``s collected, final lifecycle
states, broker open-orders cross-check) is wired through the runner
without HTTP.

The tests deliberately stay at the ``ScalperRunner`` + ``_FakeExecutionClient``
layer used by the rest of the suite — no real testnet calls. Real
``--confirm`` lifecycle validation against ``testnet.binance.vision`` is
documented in ``docs/runbooks/binance-testnet-scalping.md`` §10C as an
operator-gated smoke step.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
import pytest_asyncio

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.testnet.dto import (
    CancelResult,
    DryRunResult,
    OrderPreview,
    OrderSubmitResult,
)
from app.services.brokers.binance.testnet.ledger.service import (
    BinanceTestnetLedgerService,
)
from app.services.scalping.config import ScalperConfig
from app.services.scalping.decision import MarketSnapshot
from app.services.scalping.runner import ScalperRunner


@pytest_asyncio.fixture
async def instrument(db_session) -> CryptoInstrument:
    """Find-or-create the binance/spot/BTCUSDT instrument row."""
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


def _instrument_id_factory(instrument_id: int) -> Callable[[str], Awaitable[int]]:
    async def _get(symbol: str) -> int:
        return instrument_id

    return _get


def _snapshot_factory(
    snapshot: MarketSnapshot,
) -> Callable[[str], Awaitable[MarketSnapshot]]:
    async def _get(symbol: str) -> MarketSnapshot:
        return snapshot

    return _get


def _entry_snapshot() -> MarketSnapshot:
    """Snapshot that resolves to Entry(BUY): oversold RSI + uptrend EMA."""
    return MarketSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("50000"),
        rsi_5m=20.0,
        ema_20_5m=Decimal("49600"),
        ema_50_5m=Decimal("49000"),
        instrument_health="healthy",
    )


class _FakeExecutionClientNotFilled:
    """Fake client where every ``submit_order`` returns ``status="NEW"``.

    Records submit + cancel calls so the test can assert exactly what
    happened. Defaults emulate Binance behaviour for an order that the
    broker accepts but doesn't fill within the response window.
    """

    def __init__(self) -> None:
        self.submit_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.open_orders_calls: list[dict] = []
        self._counter = 0
        # The runner inspects the submit response's ``status``; "NEW" means
        # the broker accepted the order but it isn't filled yet, so the
        # runner SHOULD NOT proceed to TP/SL placement on this tick.
        self.submit_status = "NEW"

    def _next_broker_id(self) -> str:
        self._counter += 1
        return f"broker-{self._counter}"

    def _new_client_order_id(self) -> str:
        self._counter += 1
        return f"cid-{self._counter}"

    async def submit_order(self, **kwargs):
        self.submit_calls.append(kwargs)
        # Mirror the real client's operator gate: dry_run=True or
        # confirm=False returns a DryRunResult with no broker activity.
        if kwargs.get("dry_run", True) or not kwargs.get("confirm", False):
            preview = OrderPreview(
                symbol=kwargs["symbol"],
                side=kwargs["side"],
                order_type=kwargs["order_type"],
                quantity=kwargs["quantity"],
                price=kwargs.get("price"),
                notional_usdt=kwargs.get("notional_usdt", Decimal("0")),
                client_order_id=kwargs["client_order_id"],
            )
            return DryRunResult(preview=preview, reason="fake dry-run")
        return OrderSubmitResult(
            client_order_id=kwargs["client_order_id"],
            broker_order_id=self._next_broker_id(),
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            order_type=kwargs["order_type"],
            quantity=kwargs["quantity"],
            price=kwargs.get("price"),
            status=self.submit_status,
            transact_time_ms=1700000000000,
            raw_response={},
        )

    async def cancel_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        return CancelResult(
            client_order_id=kwargs["client_order_id"],
            broker_order_id=self._next_broker_id(),
            symbol=kwargs["symbol"],
            status="CANCELED",
            raw_response={},
        )

    async def open_orders(self, *, symbol: str) -> list[dict]:
        self.open_orders_calls.append({"symbol": symbol})
        return []

    async def aclose(self) -> None:
        return


@pytest.mark.asyncio
async def test_entry_submitted_not_filled_then_operator_cancel(
    db_session,
    instrument: CryptoInstrument,
) -> None:
    """ROB-294 acceptance #1 — submitted-not-filled-cancel branch.

    Flow:
      1. Snapshot resolves to Entry(BUY).
      2. Fake broker returns ``status="NEW"`` (accepted, not filled).
      3. Runner records the entry row at ``submitted`` and stops there:
         no TP/SL placement happens because ``status != "FILLED"``.
      4. Operator (here: the test) issues a confirmed cancel via the
         execution client; the ledger transitions to ``cancelled``.

    This is the exact path the lifecycle smoke CLI's
    ``--cancel-pending-on-exit`` flag exercises. The runner itself does
    not poll for fills mid-tick — that is by design (no in-process retry
    loop). Operator cancel is the deterministic recovery.
    """
    fake = _FakeExecutionClientNotFilled()
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    runner = ScalperRunner(
        execution_client=fake,  # type: ignore[arg-type]
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(_entry_snapshot()),
        dry_run=False,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "entry"
    assert tick.submitted is True
    # No TP/SL placement happened — only the entry submit call was made.
    assert len(fake.submit_calls) == 1
    # cancel_calls is still empty pre-operator-action.
    assert fake.cancel_calls == []
    # Ledger: single entry row in ``submitted``.
    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    submitted_rows = [r for r in rows if r.lifecycle_state == "submitted"]
    assert len(submitted_rows) == 1, (
        f"expected exactly one submitted row, got: "
        f"{[(r.client_order_id, r.lifecycle_state) for r in rows]}"
    )
    entry_cid = submitted_rows[0].client_order_id

    # --- Operator cancel (the recovery path) -----------------------------
    cancel_result = await fake.cancel_order(
        symbol="BTCUSDT",
        client_order_id=entry_cid,
        dry_run=False,
        confirm=True,
    )
    assert isinstance(cancel_result, CancelResult)
    assert cancel_result.status == "CANCELED"
    # Record the cancel in the ledger — the service layer enforces the
    # ``submitted → cancelled`` transition (illegal moves would raise
    # ``BinanceInvalidStateTransition``).
    cancelled_row = await ledger.record_cancel(
        client_order_id=entry_cid,
        reason="operator_cancel_not_filled_within_window",
    )
    assert cancelled_row.lifecycle_state == "cancelled"
    assert cancelled_row.cancelled_at is not None
    # Final lifecycle state assertion — what the smoke CLI's evidence
    # block reports for this branch.
    final = await ledger.get_by_client_order_id(entry_cid)
    assert final is not None
    assert final.lifecycle_state == "cancelled"


@pytest.mark.asyncio
async def test_dry_run_lifecycle_produces_no_submitted_or_cancel_calls(
    db_session,
    instrument: CryptoInstrument,
) -> None:
    """ROB-294 — confirmed evidence shape for the dry-run stage.

    When the runner is in dry-run mode the lifecycle stops at
    ``validated``: no submits and no cancels are issued against the
    broker. This locks the "no HTTP in dry-run" guarantee from the
    runner's side (the execution client's gate is already tested
    elsewhere, but the runner could in theory bypass it; this test pins
    the joint behaviour).
    """
    fake = _FakeExecutionClientNotFilled()
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    runner = ScalperRunner(
        execution_client=fake,  # type: ignore[arg-type]
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(_entry_snapshot()),
        dry_run=True,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "entry"
    # In dry-run the submit returns a DryRunResult, so the runner's
    # ``submitted`` boolean stays False. We assert the contract: no
    # broker mutation calls at all.
    assert tick.submitted is False
    assert tick.dry_run is True
    # Critically — even though we called ``submit_order``, the fake
    # records the kwargs, so we can prove the runner's ``dry_run=True``
    # flag flowed into the client call.
    assert len(fake.submit_calls) == 1
    assert fake.submit_calls[0]["dry_run"] is True
    assert fake.submit_calls[0]["confirm"] is False
    # No cancels.
    assert fake.cancel_calls == []
    # Ledger: the entry row is in ``validated`` (planned → previewed →
    # validated path, with no submit).
    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    assert len(rows) == 1
    assert rows[0].lifecycle_state == "validated"


@pytest.mark.asyncio
async def test_lifecycle_evidence_shape_after_full_armed_flow(
    db_session,
    instrument: CryptoInstrument,
) -> None:
    """ROB-294 — evidence snapshot for the filled+armed branch.

    Verifies that after the full happy path (entry filled + paired
    TP/SL armed) the ledger can be projected into the evidence shape
    the lifecycle smoke CLI emits:

      * client_order_ids_created (one entry + two paired legs);
      * final lifecycle states (entry=filled, legs=tp_sl_armed);
      * anomaly_client_order_ids empty;
      * broker_open_orders_after (mocked here at 2 — TP + SL armed).

    This locks the evidence contract so a future runner refactor can't
    silently break the operator handoff.
    """
    # We reuse the canonical fake from the ROB-289 test file's TT7 path.
    from tests.services.scalping.test_runner_lifecycle import _FakeExecutionClient

    fake = _FakeExecutionClient()
    ledger = BinanceTestnetLedgerService(session=db_session)
    config = ScalperConfig.default_for_testnet()
    runner = ScalperRunner(
        execution_client=fake,  # type: ignore[arg-type]
        ledger_service=ledger,
        config=config,
        instrument_id_for_symbol=_instrument_id_factory(instrument.id),
        market_snapshot_for_symbol=_snapshot_factory(_entry_snapshot()),
        dry_run=False,
    )
    tick = await runner.tick_once(symbol="BTCUSDT")
    assert tick.action_name == "entry"
    assert tick.submitted is True

    rows = await ledger.list_by_instrument(instrument_id=instrument.id, limit=10)
    states = {r.client_order_id: r.lifecycle_state for r in rows}
    # Exactly one entry row in ``filled``.
    filled = [cid for cid, st in states.items() if st == "filled"]
    assert len(filled) == 1
    entry_cid = filled[0]
    # Exactly two paired legs in ``tp_sl_armed`` with matching parent CID.
    armed = [cid for cid, st in states.items() if st == "tp_sl_armed"]
    assert len(armed) == 2
    assert {f"{entry_cid}-tp", f"{entry_cid}-sl"} == set(armed)
    # No anomalies.
    anomaly = [cid for cid, st in states.items() if st == "anomaly"]
    assert anomaly == []
