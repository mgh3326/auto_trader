"""hk 4606 — shared evidence boundary for the KIS US and Toss reconcile kernels.

Both kernels (``full`` and ``terminal_only`` scope) run broker evidence
through one boundary before any terminal transition or fill booking:
``usable`` acts, ``contradictory``/``incomplete`` leave the row open with
``reason_code`` ``evidence_contradiction``/``evidence_incomplete``.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.tooling import live_order_ledger as live
from app.mcp_server.tooling import toss_live_ledger as toss
from app.mcp_server.tooling.evidence_boundary import EvidenceBoundary
from app.mcp_server.tooling.live_order_evidence import (
    classify_live_evidence_boundary,
)
from app.mcp_server.tooling.toss_live_evidence import (
    classify_toss_evidence_boundary,
    classify_toss_order_evidence,
)
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
)
from app.services.execution_ledger.normalizers import normalize_upbit_order
from app.services.execution_ledger.recorded_fills import (
    order_identity_from_fields,
    order_identity_matches,
)
from app.services.execution_ledger.supersede import order_identity


def _fill(verdict, qty=Decimal("0"), price=None, code="test", detail=""):
    return FillEvidence(verdict, qty, price, None, code, detail)


# ---------------------------------------------------------------------------
# classify_live_evidence_boundary (KIS US / Upbit live kernel)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evidence,recorded,boundary",
    [
        # usable: ordinary no-fill terminal evidence, nothing recorded
        (_fill(FillVerdict.EXPIRED), 0, EvidenceBoundary.USABLE),
        (_fill(FillVerdict.CANCELLED), 0, EvidenceBoundary.USABLE),
        # usable: a clean fill verdict
        (
            _fill(FillVerdict.FILLED, Decimal("1"), Decimal("10")),
            0,
            EvidenceBoundary.USABLE,
        ),
        (
            _fill(FillVerdict.PARTIAL, Decimal("1"), Decimal("10")),
            0,
            EvidenceBoundary.USABLE,
        ),
        # usable: pending / none are not evidence the boundary judges
        (_fill(FillVerdict.PENDING), 0, EvidenceBoundary.USABLE),
        (_fill(FillVerdict.NONE), 0, EvidenceBoundary.USABLE),
        # contradictory: broker says terminal no-fill, fills are recorded
        (_fill(FillVerdict.EXPIRED), 3, EvidenceBoundary.CONTRADICTORY),
        (_fill(FillVerdict.CANCELLED), Decimal("0.5"), EvidenceBoundary.CONTRADICTORY),
        # incomplete: terminal verdict carrying a fill quantity
        (
            _fill(FillVerdict.EXPIRED, Decimal("1"), Decimal("10")),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
        # incomplete: fill verdicts with unusable quantities / prices
        (
            _fill(FillVerdict.FILLED, None, Decimal("10")),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
        (
            _fill(FillVerdict.FILLED, Decimal("NaN"), Decimal("10")),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
        (
            _fill(FillVerdict.FILLED, Decimal("-1"), Decimal("10")),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
        (
            _fill(FillVerdict.PARTIAL, Decimal("0"), Decimal("10")),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
        (
            _fill(FillVerdict.FILLED, Decimal("1.5"), Decimal("10")),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),  # non-integer qty on an integer-quantity broker
        (
            _fill(FillVerdict.FILLED, Decimal("1"), None),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
        (
            _fill(FillVerdict.FILLED, Decimal("1"), Decimal("0")),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
        (
            _fill(FillVerdict.FILLED, Decimal("1"), Decimal("NaN")),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
    ],
)
def test_live_boundary_classification(evidence, recorded, boundary):
    result = classify_live_evidence_boundary(
        broker="kis", evidence=evidence, recorded_fill_qty=Decimal(str(recorded))
    )
    assert result.boundary == boundary
    if boundary is EvidenceBoundary.CONTRADICTORY:
        assert result.reason_code == "evidence_contradiction"
    if boundary is EvidenceBoundary.INCOMPLETE:
        assert result.reason_code == "evidence_incomplete"


def test_live_boundary_upbit_allows_fractional_fill_qty():
    result = classify_live_evidence_boundary(
        broker="upbit",
        evidence=_fill(FillVerdict.FILLED, Decimal("1.5"), Decimal("10")),
        recorded_fill_qty=Decimal("0"),
    )
    assert result.boundary == EvidenceBoundary.USABLE


# ---------------------------------------------------------------------------
# classify_toss_evidence_boundary
# ---------------------------------------------------------------------------


def _toss_order(status, execution=None, **kw):
    return SimpleNamespace(status=status, execution=execution or {}, **kw)


@pytest.mark.parametrize(
    "order,recorded,boundary",
    [
        # usable: cancelled with an explicit zero fill
        (
            _toss_order("CANCELED", {"filledQuantity": "0"}),
            0,
            EvidenceBoundary.USABLE,
        ),
        # usable: cancelled with no execution block at all
        (_toss_order("CANCELED"), 0, EvidenceBoundary.USABLE),
        # usable: rejected
        (_toss_order("REJECTED", {"filledQuantity": "0"}), 0, EvidenceBoundary.USABLE),
        # contradictory: recorded fills vs broker terminal no-fill evidence
        (
            _toss_order("CANCELED", {"filledQuantity": "0"}),
            2,
            EvidenceBoundary.CONTRADICTORY,
        ),
        (
            _toss_order("REJECTED", {"filledQuantity": "0"}),
            2,
            EvidenceBoundary.CONTRADICTORY,
        ),
        # incomplete: terminal evidence carrying unusable broker quantities
        (
            _toss_order("CANCELED", {"filledQuantity": "abc"}),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
        (
            _toss_order("CANCELED", {"filledQuantity": "-1"}),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
        # incomplete: positive broker fill under a no-fill terminal verdict
        # (fill quantity without a usable price falls to the no-fill branch)
        (
            _toss_order("CANCELED", {"filledQuantity": "1"}),
            0,
            EvidenceBoundary.INCOMPLETE,
        ),
    ],
)
def test_toss_boundary_terminal_classification(order, recorded, boundary):
    evidence = classify_toss_order_evidence(order)
    assert evidence.verdict in {"none", "expired"}
    result = classify_toss_evidence_boundary(
        market="us", evidence=evidence, recorded_fill_qty=Decimal(str(recorded))
    )
    assert result.boundary == boundary


def test_toss_boundary_expired_contradictory_with_recorded_fill():
    order = _toss_order(
        "REJECTED",
        {"filledQuantity": "0"},
        time_in_force="DAY",
        canceled_at="2026-11-12T15:00:00+09:00",
    )
    evidence = classify_toss_order_evidence(order)
    assert evidence.verdict == "expired"
    result = classify_toss_evidence_boundary(
        market="kr", evidence=evidence, recorded_fill_qty=Decimal("1")
    )
    assert result.boundary == EvidenceBoundary.CONTRADICTORY


def test_toss_boundary_kr_fill_verdict_requires_integer_qty():
    order = _toss_order(
        "PARTIAL_FILLED",
        {"filledQuantity": "1.5", "averageFilledPrice": "10"},
    )
    evidence = classify_toss_order_evidence(order)
    assert evidence.verdict == "partial"
    kr = classify_toss_evidence_boundary(
        market="kr", evidence=evidence, recorded_fill_qty=Decimal("0")
    )
    assert kr.boundary == EvidenceBoundary.INCOMPLETE
    us = classify_toss_evidence_boundary(
        market="us", evidence=evidence, recorded_fill_qty=Decimal("0")
    )
    assert us.boundary == EvidenceBoundary.USABLE


# ---------------------------------------------------------------------------
# recorded-fill order identity
# ---------------------------------------------------------------------------


def test_order_identity_blank_order_id_never_matches():
    assert (
        order_identity_from_fields(
            broker="kis",
            instrument_type="equity_us",
            symbol="AAPL",
            side="buy",
            currency="USD",
            broker_order_id="",
            order_time=None,
        )
        is None
    )


def test_order_identity_wildcards_missing_fields():
    order = order_identity_from_fields(
        broker="toss",
        instrument_type="equity_kr",
        symbol=None,
        side="buy",
        currency=None,
        broker_order_id="000123",
        order_time=None,
    )
    fill = ("toss", "live", "equity_kr", "005930", "buy", "KRW", "123", None)
    assert order_identity_matches(order, fill)
    other_side = ("toss", "live", "equity_kr", "005930", "sell", "KRW", "123", None)
    assert not order_identity_matches(order, other_side)


def test_order_identity_normalizes_leading_zeros():
    order = order_identity_from_fields(
        broker="toss",
        instrument_type="equity_us",
        symbol="AAPL",
        side="buy",
        currency="USD",
        broker_order_id="000123",
        order_time=None,
    )
    fill = ("toss", "live", "equity_us", "AAPL", "buy", "USD", "123", None)
    assert order_identity_matches(order, fill)


def test_order_identity_kis_uses_order_time_trading_day():
    order = order_identity_from_fields(
        broker="kis",
        instrument_type="equity_us",
        symbol="AAPL",
        side="buy",
        currency="USD",
        broker_order_id="123",
        order_time=datetime(2026, 11, 12, 1, 0, tzinfo=UTC),
    )
    assert order[7] is not None  # trading day proven for KIS


def test_order_identity_crypto_matches_prefixed_order_to_stripped_fill():
    """LiveOrderLedger stores "KRW-BTC"; execution_ledger stores "BTC".

    Both must fold to one order identity or a recorded crypto fill is
    invisible to the boundary (mergewake r4575675668).
    """
    order = order_identity_from_fields(
        broker="upbit",
        instrument_type="crypto",
        symbol="KRW-BTC",  # the LiveOrderLedger stored form
        side="buy",
        currency="KRW",
        broker_order_id="uuid-1",
        order_time=None,
    )
    fill = normalize_upbit_order(
        {
            "uuid": "uuid-1",
            "market": "KRW-BTC",
            "side": "bid",
            "state": "done",
            "executed_volume": "0.5",
            "price": "60000000",
            "paid_fee": "0",
            "created_at": "2026-11-14T01:00:00",
            "trades": [],
        }
    )[0]
    fill_row = SimpleNamespace(
        broker="upbit",
        account_mode="live",
        instrument_type=fill["instrument_type"],
        symbol=fill["symbol"],  # the execution_ledger stored form
        side=fill["side"],
        currency=fill["currency"],
        broker_order_id=fill["order_id"],
        filled_at=datetime(2026, 11, 14, 1, 0, tzinfo=UTC),
        source="reconciler",
    )
    assert fill["symbol"] == "BTC"
    assert order_identity_matches(order, order_identity(fill_row))


# ---------------------------------------------------------------------------
# kernel wiring: both scopes reject contradictory / incomplete evidence
# ---------------------------------------------------------------------------


def _live_row(filled_qty=Decimal("0")):
    return SimpleNamespace(
        id=1,
        order_no="order",
        broker="kis",
        market="us",
        symbol="AAPL",
        filled_qty=filled_qty,
    )


def _toss_row(filled_qty=Decimal("0"), market="us"):
    return SimpleNamespace(
        id=2,
        broker_order_id="order",
        client_order_id="client",
        market=market,
        symbol="AAPL",
        operation_kind="place",
        status="accepted",
        filled_qty=filled_qty,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["full", "terminal_only"])
@pytest.mark.parametrize("verdict", [FillVerdict.EXPIRED, FillVerdict.CANCELLED])
async def test_live_kernel_contradiction_stays_open(verdict, scope):
    evidence = _fill(verdict)
    adapter = SimpleNamespace(fetch_evidence=AsyncMock(return_value=evidence))
    with (
        patch.object(live, "get_evidence_adapter", return_value=adapter),
        patch.object(
            live, "_recorded_positive_fill_qty", new_callable=AsyncMock
        ) as recorded,
        patch.object(
            live, "_update_live_ledger_outcome", new_callable=AsyncMock
        ) as update,
        patch.object(
            live, "_record_live_boundary_block", new_callable=AsyncMock
        ) as block,
    ):
        recorded.return_value = Decimal("3")
        out = await live._reconcile_one_live_row(
            _live_row(), dry_run=False, scope=scope
        )
    assert out["action"] == "noop_evidence_boundary"
    assert out["reason_code"] == "evidence_contradiction"
    assert out["requires_manual_review"] is True
    update.assert_not_awaited()
    block.assert_awaited_once()
    assert block.await_args.kwargs["reason_code"] == "evidence_contradiction"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["full", "terminal_only"])
async def test_live_kernel_incomplete_fill_evidence_never_books(scope):
    evidence = _fill(FillVerdict.FILLED, Decimal("1"), None)
    adapter = SimpleNamespace(fetch_evidence=AsyncMock(return_value=evidence))
    with (
        patch.object(live, "get_evidence_adapter", return_value=adapter),
        patch.object(live, "_save_order_fill", new_callable=AsyncMock) as fill,
        patch.object(
            live, "_update_live_ledger_outcome", new_callable=AsyncMock
        ) as update,
        patch.object(
            live, "_record_live_boundary_block", new_callable=AsyncMock
        ) as block,
    ):
        out = await live._reconcile_one_live_row(
            _live_row(), dry_run=False, scope=scope
        )
    assert out["action"] == "noop_evidence_boundary"
    assert out["reason_code"] == "evidence_incomplete"
    fill.assert_not_awaited()
    update.assert_not_awaited()
    block.assert_awaited_once()


def _crypto_live_row():
    return SimpleNamespace(
        id=3,
        order_no="uuid-1",
        broker="upbit",
        market="crypto",
        symbol="KRW-BTC",  # the LiveOrderLedger stored form
        side="buy",
        currency="KRW",
        trade_date=datetime(2026, 11, 14, 1, 0, tzinfo=UTC),
        filled_qty=Decimal("0"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["full", "terminal_only"])
async def test_live_kernel_crypto_recorded_fill_is_contradictory(scope):
    """The real `_recorded_positive_fill_qty` and identity matching run;
    only the session is faked (mergewake r4575675668 — before the
    `_symbol_key` fix the "KRW-BTC" order could not see its "BTC" fill and
    this row closed as cancelled)."""
    fill_row = SimpleNamespace(
        broker="upbit",
        account_mode="live",
        instrument_type="crypto",
        symbol="BTC",  # the execution_ledger stored form
        side="buy",
        currency="KRW",
        broker_order_id="uuid-1",
        filled_at=datetime(2026, 11, 14, 1, 0, tzinfo=UTC),
        filled_qty=Decimal("0.5"),
        source="reconciler",
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [fill_row]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    class _SessionCM:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    session_factory = MagicMock(return_value=MagicMock(return_value=_SessionCM()))
    evidence = _fill(FillVerdict.CANCELLED)
    adapter = SimpleNamespace(fetch_evidence=AsyncMock(return_value=evidence))
    with (
        patch.object(live, "get_evidence_adapter", return_value=adapter),
        patch.object(live, "_order_session_factory", session_factory),
        patch.object(
            live, "_update_live_ledger_outcome", new_callable=AsyncMock
        ) as update,
        patch.object(
            live, "_record_live_boundary_block", new_callable=AsyncMock
        ) as block,
    ):
        out = await live._reconcile_one_live_row(
            _crypto_live_row(), dry_run=False, scope=scope
        )
    assert out["action"] == "noop_evidence_boundary"
    assert out["reason_code"] == "evidence_contradiction"
    update.assert_not_awaited()
    block.assert_awaited_once()
    assert block.await_args.kwargs["reason_code"] == "evidence_contradiction"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["full", "terminal_only"])
async def test_toss_kernel_contradiction_stays_open(scope):
    order = _toss_order("CANCELED", {"filledQuantity": "0"})
    source = SimpleNamespace(
        evidence_for=AsyncMock(return_value=classify_toss_order_evidence(order))
    )
    svc = MagicMock()
    svc.record_transient_reconcile_error = AsyncMock()
    svc.update_reconcile_outcome = AsyncMock()
    with (
        patch.object(
            toss, "_recorded_positive_toss_fill_qty", new_callable=AsyncMock
        ) as recorded,
        patch.object(toss, "_order_session_factory"),
        patch.object(toss, "TossLiveOrderLedgerService", return_value=svc),
    ):
        recorded.return_value = Decimal("2")
        out = await toss._reconcile_one_toss_row(
            _toss_row(filled_qty=Decimal("2")),
            dry_run=False,
            evidence_source=source,
            scope=scope,
        )
    assert out["action"] == "noop_evidence_boundary"
    assert out["reason_code"] == "evidence_contradiction"
    assert out["requires_manual_review"] is True
    svc.record_transient_reconcile_error.assert_awaited_once()
    svc.update_reconcile_outcome.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["full", "terminal_only"])
async def test_toss_kernel_incomplete_quantity_never_closes(scope):
    order = _toss_order("CANCELED", {"filledQuantity": "abc"})
    source = SimpleNamespace(
        evidence_for=AsyncMock(return_value=classify_toss_order_evidence(order))
    )
    svc = MagicMock()
    svc.record_transient_reconcile_error = AsyncMock()
    svc.update_reconcile_outcome = AsyncMock()
    with (
        patch.object(
            toss, "_recorded_positive_toss_fill_qty", new_callable=AsyncMock
        ) as recorded,
        patch.object(toss, "_order_session_factory"),
        patch.object(toss, "TossLiveOrderLedgerService", return_value=svc),
    ):
        recorded.return_value = Decimal("0")
        out = await toss._reconcile_one_toss_row(
            _toss_row(), dry_run=False, evidence_source=source, scope=scope
        )
    assert out["action"] == "noop_evidence_boundary"
    assert out["reason_code"] == "evidence_incomplete"
    svc.update_reconcile_outcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_boundary_block_keeps_row_unreconciled():
    """The blocked row stays open AND inside the unreconciled-age metrics:
    ``reconciled_at`` must stay NULL (a stamp would drop it from
    ``get_unreconciled_live_order_metrics``)."""
    row = SimpleNamespace(
        status="accepted",
        lifecycle_state="accepted",
        reconciled_at=None,
        last_reconcile_detail=None,
    )
    db = MagicMock()
    db.get = AsyncMock(return_value=row)
    db.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch.object(live, "_order_session_factory", return_value=lambda: cm):
        await live._record_live_boundary_block(
            ledger_id=1,
            reason_code="evidence_contradiction",
            detail="broker expired vs recorded fills",
        )
    assert row.reconciled_at is None
    assert row.status == "accepted"
    assert row.last_reconcile_detail["reason_code"] == "evidence_contradiction"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_toss_kernel_boundary_is_scope_independent():
    """A mutant bypassing the boundary in one scope turns this test red."""
    order = _toss_order("CANCELED", {"filledQuantity": "abc"})
    source = SimpleNamespace(
        evidence_for=AsyncMock(return_value=classify_toss_order_evidence(order))
    )
    with patch.object(
        toss, "_recorded_positive_toss_fill_qty", new_callable=AsyncMock
    ) as recorded:
        recorded.return_value = Decimal("0")
        full = await toss._reconcile_one_toss_row(
            _toss_row(), dry_run=True, evidence_source=source
        )
        terminal = await toss._reconcile_one_toss_row(
            _toss_row(),
            dry_run=True,
            evidence_source=source,
            scope="terminal_only",
        )
    assert terminal == full
    assert full["reason_code"] == "evidence_incomplete"
