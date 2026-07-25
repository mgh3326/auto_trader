"""Tests for KIS mock reconciliation job composition (ROB-102)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation
from app.mcp_server.tooling.kis_mock_ledger import _shadow_row_to_order
from app.models.review import KISMockOrderLedger


def _ledger_row(
    *,
    ledger_id: int = 101,
    symbol: str = "005930",
    side: str = "buy",
    qty: Decimal = Decimal("10"),
    state: str = "accepted",
    baseline: Decimal | None = Decimal("5"),
    accepted_age_sec: int = 5,
    instrument_type: str = "equity_kr",
    price: Decimal = Decimal("0"),
):
    row = MagicMock(spec=KISMockOrderLedger)
    row.id = ledger_id
    row.symbol = symbol
    row.side = side
    row.quantity = qty
    row.lifecycle_state = state
    row.holdings_baseline_qty = baseline
    row.trade_date = datetime.now(UTC) - timedelta(seconds=accepted_age_sec)
    row.instrument_type = instrument_type
    row.price = price
    return row


def _fake_kis_client(*, kr=None, us=None):
    client = MagicMock()
    client.fetch_my_stocks = AsyncMock(side_effect=[kr or [], us or []])
    return client


def _fake_kis_client_seq(*, side_effect):
    """KIS client whose fetch_my_stocks resolves KR then US via an explicit
    side_effect list (entries may be lists or Exception instances to fail a
    market fetch)."""
    client = MagicMock()
    client.fetch_my_stocks = AsyncMock(side_effect=side_effect)
    return client


@pytest.mark.asyncio
async def test_reconciliation_job_uses_kis_mock_holdings(monkeypatch):
    """Job must call fetch_my_stocks(is_mock=True) for both KR and US."""
    mock_db = AsyncMock()

    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [_ledger_row()]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    # Real KIS contract: KR uses pdno/hldg_qty, US uses ovrs_pdno/ovrs_cblc_qty.
    fake_kis = _fake_kis_client(kr=[{"pdno": "005930", "hldg_qty": "15"}])

    result = await run_kis_mock_reconciliation(
        mock_db, dry_run=False, kis_client=fake_kis
    )

    assert result["orders_processed"] == 1
    assert result["transitions_applied"] == 1
    assert result["account_mode"] == "kis_mock"

    # Verify both KR and US holdings calls were issued with is_mock=True.
    assert fake_kis.fetch_my_stocks.await_count == 2
    kr_call = fake_kis.fetch_my_stocks.await_args_list[0]
    us_call = fake_kis.fetch_my_stocks.await_args_list[1]
    assert kr_call.kwargs == {"is_mock": True, "is_overseas": False}
    assert us_call.kwargs == {"is_mock": True, "is_overseas": True}

    # Verify transition was applied to the right ledger row.
    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["ledger_id"] == 101
    assert args["next_state"] == "fill"
    assert args["reason_code"] == "fill_detected"
    assert args["dry_run"] is False


@pytest.mark.asyncio
async def test_reconciliation_job_handles_overseas_holdings(monkeypatch):
    mock_db = AsyncMock()

    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [
        _ledger_row(ledger_id=202, symbol="AAPL")
    ]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(
        kr=[],
        us=[{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "15"}],
    )

    result = await run_kis_mock_reconciliation(
        mock_db, dry_run=False, kis_client=fake_kis
    )

    assert result["transitions_applied"] == 1
    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["next_state"] == "fill"


@pytest.mark.asyncio
async def test_reconciliation_job_emits_lifecycle_events(monkeypatch):
    mock_db = AsyncMock()

    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [_ledger_row()]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(kr=[{"pdno": "005930", "hldg_qty": "15"}])

    result = await run_kis_mock_reconciliation(
        mock_db, dry_run=True, kis_client=fake_kis
    )

    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["account_mode"] == "kis_mock"
    assert event["execution_source"] == "reconciler"
    assert event["state"] == "fill"
    assert event["detail"]["ledger_id"] == 101


@pytest.mark.asyncio
async def test_reconciliation_job_no_open_orders(monkeypatch):
    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = []
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )
    fake_kis = _fake_kis_client()

    result = await run_kis_mock_reconciliation(
        mock_db, dry_run=True, kis_client=fake_kis
    )
    assert result["orders_processed"] == 0
    fake_kis.fetch_my_stocks.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_attributes_single_delta_and_records_attributed_qty(
    monkeypatch,
):
    row23 = _ledger_row(
        ledger_id=23,
        symbol="0148J0",
        side="buy",
        qty=Decimal("10"),
        state="accepted",
        baseline=Decimal("0"),
        accepted_age_sec=120,
    )
    row23.price = Decimal("15500")

    row24 = _ledger_row(
        ledger_id=24,
        symbol="0148J0",
        side="buy",
        qty=Decimal("10"),
        state="accepted",
        baseline=Decimal("0"),
        accepted_age_sec=60,
    )
    row24.price = Decimal("15900")

    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [row23, row24]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(kr=[{"pdno": "0148J0", "hldg_qty": "10"}])

    result = await run_kis_mock_reconciliation(
        mock_db, dry_run=True, kis_client=fake_kis
    )

    events = {e["detail"]["ledger_id"]: e for e in result["events"]}
    assert events[24]["state"] == "fill"
    assert events[23]["state"] == "pending"

    # attributed_fill_qty is recorded in the applied detail / event payload
    assert events[24]["detail"]["attributed_fill_qty"] == "10"
    assert events[23]["detail"]["attributed_fill_qty"] == "0"


@pytest.mark.asyncio
async def test_attributed_fill_qty_roundtrips_into_shadow_order_history(monkeypatch):
    """Cross-seam (ROB-400 Fix #3): the exact detail the job hands the
    persistence layer (str(Decimal) ``attributed_fill_qty``) is read back by the
    shadow order-history reader without contradicting ``lifecycle_state``.

    This closes the writer/reader contract — a rename on either side breaks it,
    whereas the isolated reconciler and shadow tests would each still pass.
    """
    row24 = _ledger_row(
        ledger_id=24,
        symbol="0148J0",
        side="buy",
        qty=Decimal("10"),
        state="accepted",
        baseline=Decimal("0"),
        accepted_age_sec=60,
    )
    row24.price = Decimal("15900")

    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [row24]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(kr=[{"pdno": "0148J0", "hldg_qty": "10"}])

    await run_kis_mock_reconciliation(mock_db, dry_run=False, kis_client=fake_kis)

    # Reconstruct exactly what KISMockLifecycleService.apply_lifecycle_transition
    # persists into row.last_reconcile_detail: {"reason_code", **detail}.
    call = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    persisted_detail = {"reason_code": call["reason_code"], **call["detail"]}

    shadow_row = MagicMock(spec=KISMockOrderLedger)
    shadow_row.id = 24
    shadow_row.order_no = None
    shadow_row.symbol = "0148J0"
    shadow_row.instrument_type = "equity_kr"
    shadow_row.side = "buy"
    shadow_row.order_type = "limit"
    shadow_row.quantity = Decimal("10")
    shadow_row.price = Decimal("15900")
    shadow_row.amount = Decimal("159000")
    shadow_row.currency = "KRW"
    shadow_row.trade_date = datetime.now(UTC)
    shadow_row.lifecycle_state = call["next_state"]
    shadow_row.last_reconcile_detail = persisted_detail

    out = _shadow_row_to_order(shadow_row)
    assert out["lifecycle_state"] == "fill"
    assert out["status"] == "filled"
    assert out["filled_qty"] == 10.0
    assert out["remaining_qty"] == 0.0


@pytest.mark.asyncio
async def test_run_passes_symbol_to_list_open_orders(db_session, monkeypatch):
    from app.services.kis_mock_lifecycle_service import KISMockLifecycleService

    captured: dict = {}

    async def _fake_list_open_orders(self, *, limit=100, symbol=None, **kw):
        captured["symbol"] = symbol
        captured["limit"] = limit
        return []  # empty → run short-circuits before broker/holdings

    monkeypatch.setattr(
        KISMockLifecycleService, "list_open_orders", _fake_list_open_orders
    )
    result = await run_kis_mock_reconciliation(
        db_session, symbol="005930", dry_run=True
    )
    assert captured["symbol"] == "005930"
    assert result["orders_processed"] == 0


@pytest.mark.asyncio
async def test_run_passes_market_as_instrument_type_to_list_open_orders(
    db_session, monkeypatch
):
    """ROB-1018: ``market`` must reach ``list_open_orders`` as the
    ``instrument_type`` filter kwarg (the query-layer filter already existed;
    only the plumbing to reach it was missing)."""
    from app.services.kis_mock_lifecycle_service import KISMockLifecycleService

    captured: dict = {}

    async def _fake_list_open_orders(
        self, *, limit=100, symbol=None, instrument_type=None, **kw
    ):
        captured["symbol"] = symbol
        captured["instrument_type"] = instrument_type
        captured["limit"] = limit
        return []

    monkeypatch.setattr(
        KISMockLifecycleService, "list_open_orders", _fake_list_open_orders
    )
    result = await run_kis_mock_reconciliation(
        db_session, market="equity_us", symbol="AVGO", dry_run=True
    )
    assert captured["instrument_type"] == "equity_us"
    assert captured["symbol"] == "AVGO"
    assert result["orders_processed"] == 0
    assert result["scope"] == {"market": "equity_us", "symbol": "AVGO"}


@pytest.mark.asyncio
async def test_market_scope_excludes_out_of_scope_rows_end_to_end(monkeypatch):
    """ROB-1018 core repro: a US-scoped run must never see KR rows as
    transition candidates — the query layer (not just filtering after the
    fact) must exclude them, so a US session cannot stale-transition a
    resting KR order it never asked about.

    Mirrors the actual incident shape (ledger 63/64 KR + 65 US all open at
    once): the fake ``list_open_orders`` below returns BOTH a KR row (63) and
    a US row (65) whenever ``instrument_type`` is None, and filters down to
    just the matching market otherwise — exactly like the real DB query. A
    run scoped to ``market="equity_us"`` must end up with ONLY the US row
    in ``events``/transitions; the KR row must never surface as a candidate.

    This is a load-bearing regression guard for the bug this ticket fixed: if
    the job stops forwarding ``market`` to ``list_open_orders`` (e.g.
    regresses to passing ``instrument_type=None``), the fake below starts
    returning the KR row too and this test goes RED — a single-row fake
    would have let that regression through silently (it did, pre-fix).
    """
    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()

    async def _list_open_orders(
        *, limit=100, symbol=None, instrument_type=None, ledger_ids=None
    ):
        # Simulate the real query-layer filter: instrument_type gates which
        # rows come back. Both a KR and a US order are open simultaneously.
        rows = [
            _ledger_row(
                ledger_id=63,
                symbol="005930",
                side="buy",
                qty=Decimal("10"),
                state="accepted",
                baseline=Decimal("0"),
                instrument_type="equity_kr",
            ),
            _ledger_row(
                ledger_id=65,
                symbol="AVGO",
                side="buy",
                qty=Decimal("1"),
                state="accepted",
                baseline=Decimal("0"),
                instrument_type="equity_us",
            ),
        ]
        return [
            r
            for r in rows
            if instrument_type is None or r.instrument_type == instrument_type
        ]

    mock_lifecycle_svc.list_open_orders.side_effect = _list_open_orders
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(kr=[], us=[])

    result = await run_kis_mock_reconciliation(
        mock_db, market="equity_us", dry_run=True, kis_client=fake_kis
    )

    assert result["orders_processed"] == 1
    ledger_ids = {e["detail"]["ledger_id"] for e in result["events"]}
    assert ledger_ids == {65}
    assert 63 not in ledger_ids  # KR row must never appear as a candidate

    transitioned_ids = {
        c.kwargs["ledger_id"]
        for c in mock_lifecycle_svc.apply_lifecycle_transition.call_args_list
    }
    assert transitioned_ids == {65}

    assert result["scope"] == {"market": "equity_us", "symbol": None}


@pytest.mark.asyncio
async def test_scope_defaults_to_none_preserves_full_scan(monkeypatch):
    """ROB-1018: omitting market/symbol must keep the existing full-batch
    behavior — both reach list_open_orders as None."""
    from app.services.kis_mock_lifecycle_service import KISMockLifecycleService

    captured: dict = {}

    async def _fake_list_open_orders(
        self, *, limit=100, symbol=None, instrument_type=None, **kw
    ):
        captured["symbol"] = symbol
        captured["instrument_type"] = instrument_type
        return []

    monkeypatch.setattr(
        KISMockLifecycleService, "list_open_orders", _fake_list_open_orders
    )
    mock_db = AsyncMock()
    result = await run_kis_mock_reconciliation(mock_db, dry_run=True)
    assert captured["symbol"] is None
    assert captured["instrument_type"] is None
    assert result["scope"] == {"market": None, "symbol": None}


@pytest.mark.asyncio
async def test_sell_to_zero_books_fill_when_scoped_by_market(monkeypatch):
    """ROB-1018 x ROB-910 non-regression: scoping the order query by market
    must NOT weaken holdings verification. A scoped US sell-to-zero still
    books a fill because ``_collect_kis_mock_holdings`` always fetches BOTH
    KR and US regardless of the order-query scope."""
    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [
        _ledger_row(
            ledger_id=54,
            symbol="F",
            side="sell",
            qty=Decimal("1"),
            state="accepted",
            baseline=Decimal("1"),
            instrument_type="equity_us",
        )
    ]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    # Both fetches succeed; US returns empty (F is now zero → not listed).
    fake_kis = _fake_kis_client(kr=[], us=[])

    result = await run_kis_mock_reconciliation(
        mock_db, market="equity_us", dry_run=True, kis_client=fake_kis
    )

    # Both markets were still fetched — scoping orders never scopes holdings.
    assert fake_kis.fetch_my_stocks.await_count == 2
    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["ledger_id"] == 54
    assert args["next_state"] == "fill"
    assert args["reason_code"] == "fill_detected"
    assert result["scope"] == {"market": "equity_us", "symbol": None}


@pytest.mark.asyncio
async def test_sell_to_zero_stays_anomaly_when_scoped_and_market_fetch_fails(
    monkeypatch,
):
    """ROB-1018 x ROB-910 non-regression: scoping must not cause a failed
    market fetch to be silently treated as a verified zero. Fail-closed
    (anomaly / holdings_snapshot_missing) is preserved under scope."""
    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [
        _ledger_row(
            ledger_id=54,
            symbol="F",
            side="sell",
            qty=Decimal("1"),
            state="accepted",
            baseline=Decimal("1"),
            instrument_type="equity_us",
        )
    ]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    # KR succeeds ([]), US fetch RAISES → US market unverified, even though
    # the run is scoped to equity_us.
    fake_kis = _fake_kis_client_seq(
        side_effect=[[], RuntimeError("EGW00201 mock inquiry failed")]
    )

    result = await run_kis_mock_reconciliation(
        mock_db, market="equity_us", dry_run=True, kis_client=fake_kis
    )

    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["ledger_id"] == 54
    assert args["next_state"] == "anomaly"
    assert args["reason_code"] == "holdings_snapshot_missing"
    assert result["scope"] == {"market": "equity_us", "symbol": None}


@pytest.mark.asyncio
async def test_sell_to_zero_books_fill_when_symbol_absent_but_fetch_ok(monkeypatch):
    """ROB-910 core reproduction: full sell to zero.

    Baseline 1, sell 1. KIS holdings inquiry succeeds (US market) but returns NO
    row for the symbol because the position is now zero (KIS only returns
    nonzero holdings). Expected: next_state=fill, observed_holdings_qty=0,
    observed_delta=-1, attributed_fill_qty=1 — NOT anomaly.
    """
    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [
        _ledger_row(
            ledger_id=54,
            symbol="F",
            side="sell",
            qty=Decimal("1"),
            state="accepted",
            baseline=Decimal("1"),
            instrument_type="equity_us",
        )
    ]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    # Both fetches succeed; US returns empty (F is now zero → not listed).
    fake_kis = _fake_kis_client(kr=[], us=[])

    await run_kis_mock_reconciliation(mock_db, dry_run=True, kis_client=fake_kis)

    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["ledger_id"] == 54
    assert args["next_state"] == "fill"
    assert args["reason_code"] == "fill_detected"
    assert args["detail"]["observed_holdings_qty"] == "0"
    assert args["detail"]["observed_delta"] == "-1"
    assert args["detail"]["attributed_fill_qty"] == "1"


@pytest.mark.asyncio
async def test_sell_to_zero_stays_anomaly_when_market_fetch_fails(monkeypatch):
    """ROB-910 fail-closed boundary: if the symbol's market fetch RAISED, we must
    NOT synthesize a zero snapshot. The order stays anomaly /
    holdings_snapshot_missing (fetch failure != qty 0)."""
    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [
        _ledger_row(
            ledger_id=54,
            symbol="F",
            side="sell",
            qty=Decimal("1"),
            state="accepted",
            baseline=Decimal("1"),
            instrument_type="equity_us",
        )
    ]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    # KR succeeds ([]), US fetch RAISES → US market unverified.
    fake_kis = _fake_kis_client_seq(
        side_effect=[[], RuntimeError("EGW00201 mock inquiry failed")]
    )

    await run_kis_mock_reconciliation(mock_db, dry_run=True, kis_client=fake_kis)

    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["ledger_id"] == 54
    assert args["next_state"] == "anomaly"
    assert args["reason_code"] == "holdings_snapshot_missing"


@pytest.mark.asyncio
async def test_sell_to_zero_stays_anomaly_when_fetch_returns_none(monkeypatch):
    """ROB-910 fail-closed: a None return (not a real empty list) is treated as an
    unverified fetch — no zero synthesis, anomaly preserved."""
    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [
        _ledger_row(
            ledger_id=55,
            symbol="F",
            side="sell",
            qty=Decimal("1"),
            state="accepted",
            baseline=Decimal("1"),
            instrument_type="equity_us",
        )
    ]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client_seq(side_effect=[[], None])

    await run_kis_mock_reconciliation(mock_db, dry_run=True, kis_client=fake_kis)

    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["next_state"] == "anomaly"
    assert args["reason_code"] == "holdings_snapshot_missing"


@pytest.mark.asyncio
async def test_empty_account_books_sell_to_zero_fill(monkeypatch):
    """ROB-910: genuinely empty account (fetch succeeds, returns []) is a verified
    zero for a KR sell-to-zero — books the fill, distinct from a fetch failure."""
    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [
        _ledger_row(
            ledger_id=60,
            symbol="005930",
            side="sell",
            qty=Decimal("2"),
            state="accepted",
            baseline=Decimal("2"),
            instrument_type="equity_kr",
        )
    ]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(kr=[], us=[])

    await run_kis_mock_reconciliation(mock_db, dry_run=True, kis_client=fake_kis)

    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["next_state"] == "fill"
    assert args["reason_code"] == "fill_detected"
    assert args["detail"]["attributed_fill_qty"] == "2"


@pytest.mark.asyncio
async def test_buy_absent_symbol_not_misbooked_as_fill(monkeypatch):
    """ROB-910 buy guard: a BUY whose symbol is absent from a verified fetch
    reflects observed qty 0 (delta 0 vs baseline 0) → still pending, never a
    fabricated fill."""
    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [
        _ledger_row(
            ledger_id=70,
            symbol="0148J0",
            side="buy",
            qty=Decimal("10"),
            state="accepted",
            baseline=Decimal("0"),
            instrument_type="equity_kr",
            accepted_age_sec=5,
        )
    ]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(kr=[], us=[])

    await run_kis_mock_reconciliation(mock_db, dry_run=True, kis_client=fake_kis)

    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["next_state"] == "pending"
    assert args["reason_code"] == "pending_unconfirmed"
    assert args["detail"]["attributed_fill_qty"] == "0"


def test_reconcile_gate_flags_default_false():
    from app.core.config import settings

    assert settings.KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED is False
    assert settings.KIS_MOCK_RECONCILE_PERIODIC_ENABLED is False
