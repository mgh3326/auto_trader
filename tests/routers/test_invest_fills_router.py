"""Tests for the read-only /invest/fills router (ROB-211 K2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _ledger_row(**overrides):
    """Minimal ExecutionLedger-like namespace for DB mocking."""
    defaults = {
        "id": 1,
        "broker": "kis",
        "account_mode": "live",
        "venue": "kis_domestic",
        "instrument_type": "equity_kr",
        "symbol": "005930",
        "raw_symbol": "005930",
        "side": "buy",
        "broker_order_id": "ord-0001",
        "fill_seq": 0,
        "filled_qty": Decimal("10"),
        "filled_price": Decimal("70000"),
        "filled_notional": Decimal("700000"),
        "fee_amount": Decimal("350"),
        "fee_currency": "KRW",
        "filled_at": datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
        "currency": "KRW",
        "correlation_id": None,
        "source": "reconciler",
        "source_run_id": None,
        "created_at": datetime(2026, 5, 10, 9, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 10, 9, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _reconcile_run_row(broker: str = "kis"):
    """Minimal ExecutionLedgerReconcileRun-like namespace with recent finished_at."""
    now = datetime.now(UTC)
    return SimpleNamespace(
        run_id=uuid.uuid4(),
        broker=broker,
        window_start=now - timedelta(hours=2),
        window_end=now - timedelta(hours=1),
        started_at=now - timedelta(hours=1),
        finished_at=now - timedelta(minutes=10),  # 10 min ago → fresh
        dry_run=False,
        would_insert=1,
        would_update=0,
        unchanged=0,
        committed_insert=1,
        committed_update=0,
        error_summary=None,
        notes=None,
    )


def _make_db(data_rows, reconcile_rows, history_rows=None):
    """Return an AsyncSession-like mock serving ledger/history/freshness queries.

    First call → data rows (main ledger query).
    Optional second call → history_rows (sell-history cost-basis query).
    Remaining calls → reconcile_rows (latest_run_per_broker subquery).
    """

    def _scalars_for(rows):
        class _Scalars:
            def all(self):
                return rows

        class _Result:
            def scalars(self):
                return _Scalars()

            def scalar_one_or_none(self):
                return rows[0] if rows else None

        return _Result()

    call_count = 0

    def _execute(stmt):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalars_for(data_rows)
        if history_rows is not None and call_count == 2:
            return _scalars_for(history_rows)
        return _scalars_for(reconcile_rows)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db


def _make_app(db):
    from app.core.db import get_db
    from app.routers import invest_fills
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(invest_fills.router)
    fake_user = SimpleNamespace(id=1)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: db
    return app


# ---------------------------------------------------------------------------
# /recent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recent_fills_returns_200_with_items():
    row = _ledger_row()
    run = _reconcile_run_row("kis")
    db = _make_db([row], [run])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["items"][0]["symbol"] == "005930"
    assert data["items"][0]["source"] == "reconciler"
    assert data["items"][0]["trade_day_kst"] == "20260510"
    # K2: enriched fields
    assert "data_state" in data
    assert "source_breakdown" in data
    assert data["source_breakdown"]["reconciler"] == 1
    assert "empty_reason" in data
    assert data["empty_reason"] is None  # items present → no empty reason


@pytest.mark.unit
def test_recent_fills_empty_missing_state():
    run_none_db = _make_db([], [])  # no rows, no reconcile runs
    client = TestClient(_make_app(run_none_db))

    resp = client.get("/trading/api/invest/fills/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["items"] == []
    assert data["data_state"] == "missing"
    assert data["empty_reason"] == "no reconcile data available yet"


@pytest.mark.unit
def test_recent_fills_source_breakdown_websocket():
    row = _ledger_row(source="websocket", broker="upbit", instrument_type="crypto")
    run = _reconcile_run_row("upbit")
    db = _make_db([row], [run])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/recent?market=crypto")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_breakdown"]["websocket"] == 1
    assert data["source_breakdown"]["reconciler"] == 0


@pytest.mark.unit
def test_recent_fills_supersedes_websocket_duplicate():
    """A reconciler + websocket row for the same order collapse to the reconciler row."""
    rows = [
        _ledger_row(
            id=1, broker_order_id="0006366300", fill_seq=1511940115, source="reconciler"
        ),
        _ledger_row(
            id=2, broker_order_id="0006366300", fill_seq=654241537, source="websocket"
        ),
    ]
    run = _reconcile_run_row("kis")
    db = _make_db(rows, [run])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["source"] == "reconciler"
    assert data["source_breakdown"]["websocket"] == 0
    assert data["source_breakdown"]["reconciler"] == 1


@pytest.mark.unit
def test_recent_fills_accepts_side_filter():
    buy = _ledger_row(id=1, side="buy", broker_order_id="buy-1")
    run = _reconcile_run_row("kis")
    db = _make_db([buy], [run])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/recent?side=buy")

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["items"][0]["side"] == "buy"
    assert data["items"][0]["broker_order_id"] == "buy-1"


@pytest.mark.unit
def test_recent_fills_rejects_unknown_side():
    db = _make_db([], [])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/recent?side=hold")

    assert resp.status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recent_fills_side_filter_is_applied_before_limit():
    older_buy = _ledger_row(
        id=2,
        side="buy",
        broker_order_id="buy-old",
        filled_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
    )

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    executed = []

    async def _execute(stmt):
        executed.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        if len(executed) == 1:
            return _Result([older_buy])
        return _Result([_reconcile_run_row("kis")])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)

    from app.services.execution_ledger.query_service import ExecutionLedgerQueryService

    response = await ExecutionLedgerQueryService(db).list_recent(limit=1, side="buy")

    assert response.count == 1
    assert response.items[0].broker_order_id == "buy-old"
    assert "execution_ledger.side = 'buy'" in executed[0]
    assert "LIMIT 3" in executed[0]


# ---------------------------------------------------------------------------
# /by-symbol/{symbol}
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fills_by_symbol_returns_200_with_items():
    row = _ledger_row()
    run = _reconcile_run_row("kis")
    db = _make_db([row], [run])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/by-symbol/005930")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["items"][0]["symbol"] == "005930"


@pytest.mark.unit
def test_fills_by_symbol_empty_returns_200_not_404():
    """K2: empty symbol result returns 200 with empty_reason, not 404."""
    db = _make_db([], [])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/by-symbol/UNKNOWN")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["items"] == []
    assert data["empty_reason"] is not None


@pytest.mark.unit
def test_fills_by_symbol_with_fresh_run_shows_stale_not_missing():
    """When reconcile ran recently but no fills for symbol, state is fresh/stale."""
    # Provide runs for both brokers so overall state resolves to "fresh"
    kis_run = _reconcile_run_row("kis")
    upbit_run = _reconcile_run_row("upbit")
    db = _make_db([], [kis_run, upbit_run])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/by-symbol/NEWSTOCK")
    assert resp.status_code == 200
    data = resp.json()
    # Data was reconciled recently → fresh, but no fills for this specific symbol
    assert data["data_state"] == "fresh"
    assert data["empty_reason"] is not None


# ---------------------------------------------------------------------------
# /sell-history
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sell_history_returns_200_with_sell_rows():
    buy = _ledger_row(
        id=10,
        side="buy",
        broker_order_id="buy-001",
        filled_qty=Decimal("10"),
        filled_price=Decimal("60000"),
        filled_notional=Decimal("600000"),
        filled_at=datetime(2026, 5, 9, 9, 0, tzinfo=UTC),
    )
    row = _ledger_row(
        side="sell",
        broker_order_id="sell-001",
        filled_qty=Decimal("10"),
        filled_price=Decimal("70000"),
        filled_notional=Decimal("700000"),
        filled_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
    )
    run = _reconcile_run_row("kis")
    db = _make_db([row], [run], history_rows=[buy, row])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/sell-history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["items"][0]["side"] == "sell"
    assert data["items"][0]["realized_profit"] == "100000"
    assert data["items"][0]["realized_profit_rate"].startswith("16.666")
    assert "source_breakdown" in data
    assert "data_state" in data


@pytest.mark.unit
def test_sell_history_empty_with_no_runs():
    db = _make_db([], [])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/sell-history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["data_state"] == "missing"
    assert data["empty_reason"] == "no reconcile data available yet"


@pytest.mark.unit
def test_sell_history_market_filter_param_accepted():
    buy = _ledger_row(
        id=10,
        side="buy",
        broker="upbit",
        instrument_type="crypto",
        venue="upbit",
        symbol="KRW-BTC",
        raw_symbol="KRW-BTC",
        broker_order_id="buy-crypto-001",
        filled_qty=Decimal("0.01000000"),
        filled_price=Decimal("100000000"),
        filled_notional=Decimal("1000000"),
        filled_at=datetime(2026, 5, 9, 9, 0, tzinfo=UTC),
    )
    row = _ledger_row(
        side="sell",
        instrument_type="crypto",
        broker="upbit",
        venue="upbit",
        symbol="KRW-BTC",
        raw_symbol="KRW-BTC",
        broker_order_id="sell-crypto-001",
        filled_qty=Decimal("0.01000000"),
        filled_price=Decimal("110000000"),
        filled_notional=Decimal("1100000"),
        filled_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
    )
    run = _reconcile_run_row("upbit")
    db = _make_db([row], [run], history_rows=[buy, row])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/sell-history?market=crypto&days=7")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /freshness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_freshness_returns_both_brokers():
    """Freshness report covers both KIS and Upbit brokers."""
    kis_run = _reconcile_run_row("kis")
    upbit_run = _reconcile_run_row("upbit")

    class _Scalars:
        def all(self):
            return [kis_run, upbit_run]

    class _Result:
        def scalars(self):
            return _Scalars()

        def scalar_one_or_none(self):
            return kis_run

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())

    from app.core.db import get_db
    from app.routers import invest_fills
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(invest_fills.router)
    fake_user = SimpleNamespace(id=1)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    resp = client.get("/trading/api/invest/fills/freshness")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert len(data["items"]) == 2  # one per broker
    brokers = {item["broker"] for item in data["items"]}
    assert brokers == {"kis", "upbit"}


@pytest.mark.unit
def test_freshness_missing_when_no_reconcile_runs():
    """When no reconcile runs exist, all brokers are 'missing'."""

    class _Scalars:
        def all(self):
            return []

    class _Result:
        def scalars(self):
            return _Scalars()

        def scalar_one_or_none(self):
            return None

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())

    from app.core.db import get_db
    from app.routers import invest_fills
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(invest_fills.router)
    fake_user = SimpleNamespace(id=1)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    resp = client.get("/trading/api/invest/fills/freshness")
    assert resp.status_code == 200
    data = resp.json()
    for item in data["items"]:
        assert item["dataState"] == "missing"


@pytest.mark.unit
def test_sell_history_dedups_before_limit_and_reports_true_total():
    """De-dup runs over the full window before trimming: the websocket dup never
    leaks at the page boundary, and count is the true de-duped window total."""
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    rows = [
        _ledger_row(
            id=2,
            side="sell",
            broker_order_id="0006366300",
            fill_seq=222,
            source="websocket",
            filled_at=base + timedelta(minutes=2),
            filled_notional=Decimal("2510000"),
        ),
        _ledger_row(
            id=1,
            side="sell",
            broker_order_id="0006366300",
            fill_seq=111,
            source="reconciler",
            filled_at=base,
            filled_notional=Decimal("2510000"),
        ),
        _ledger_row(
            id=3,
            side="sell",
            broker_order_id="0000342400",
            fill_seq=333,
            source="reconciler",
            filled_at=base - timedelta(days=1),
            filled_notional=Decimal("7800000"),
        ),
        _ledger_row(
            id=4,
            side="sell",
            broker_order_id="0019990600",
            fill_seq=444,
            source="reconciler",
            filled_at=base - timedelta(days=2),
            filled_notional=Decimal("262500"),
        ),
    ]
    run = _reconcile_run_row("kis")
    db = _make_db(rows, [run], history_rows=rows)
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/sell-history?limit=2")
    assert resp.status_code == 200
    data = resp.json()
    # 4 raw rows -> 3 distinct sells after supersede; count is the true total.
    assert data["count"] == 3
    assert len(data["items"]) == 2  # trimmed page
    assert all(item["source"] == "reconciler" for item in data["items"])
    assert data["source_breakdown"]["websocket"] == 0
