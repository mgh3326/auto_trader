"""Domestic fillwire frames must reach the notifier with real fill values."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.routers import execution_ledger_ingest as ingest_module
from app.routers.execution_ledger_ingest import _rebuild_fill_order
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.schemas.execution_ledger_ingest import ExecutionLedgerFillIngestRequest
from app.services.execution_ledger.fill_ingest import send_fill_notification
from app.services.fill_notification import is_fill_notifiable, normalize_kis_fill
from tests.fixtures.execution_ledger_fill_frames import fillwire_kis_domestic_frame

pytestmark = pytest.mark.unit


def _fill(raw: dict | None) -> ExecutionLedgerUpsert:
    return ExecutionLedgerUpsert(
        broker="kis",
        account_mode="live",
        venue="krx",
        instrument_type="equity_kr",
        symbol="005930",
        raw_symbol="005930",
        side="buy",
        broker_order_id="0000123456",
        fill_seq=7,
        filled_qty="10",
        filled_price="70000",
        filled_at="2026-11-02T00:30:00Z",
        currency="KRW",
        source="websocket",
        raw_payload_json=raw,
    )


@pytest.mark.parametrize("raw_kind", ["canonical", "python_parsed", "fillwire"])
def test_reproduce_notification_gap(raw_kind: str) -> None:
    """The original external repro, now asserting the intended behavior."""
    raw = {
        "canonical": None,
        "python_parsed": {
            "symbol": "005930",
            "side": "buy",
            "market": "kr",
            "filled_qty": 10,
            "filled_price": 70000,
            "fill_yn": "2",
        },
        "fillwire": fillwire_kis_domestic_frame(),
    }[raw_kind]
    order = _rebuild_fill_order(_fill(raw))
    assert (
        order.symbol,
        order.filled_qty,
        order.filled_price,
        is_fill_notifiable(order),
    ) == (
        "005930",
        10.0,
        70000.0,
        True,
    )


@pytest.mark.parametrize("tr", ["H0STCNI0", "H0STCNI9"])
@pytest.mark.parametrize("side,expected", [("01", "ask"), ("02", "bid")])
def test_normalizer_reads_official_domestic_fields(
    tr: str, side: str, expected: str
) -> None:
    raw = fillwire_kis_domestic_frame(tr=tr, dup_suspect=True, dup_observation_count=2)
    raw["fields"][4] = side
    order = normalize_kis_fill(raw)
    assert order.symbol == "005930"
    assert order.order_id == "0000123456"
    assert order.side == expected
    assert order.filled_qty == 10
    assert order.filled_price == 70000
    assert order.filled_amount == 700000
    assert order.market_type == "kr"
    assert order.currency == "KRW"
    assert datetime.fromisoformat(order.filled_at) == datetime(
        2026, 11, 2, 0, 30, tzinfo=UTC
    )


@pytest.mark.asyncio
async def test_fillwire_domestic_fill_reaches_the_notifier() -> None:
    notifier = AsyncMock()
    notifier.notify_fill.return_value = True
    order = _rebuild_fill_order(_fill(fillwire_kis_domestic_frame()))
    delivered = await send_fill_notification(
        order,
        notifier_factory=lambda: notifier,
        enrichment_fetcher=AsyncMock(return_value=None),
    )
    assert delivered is True
    notifier.notify_fill.assert_awaited_once()
    assert notifier.notify_fill.call_args.args[0].filled_amount == 700000


@pytest.mark.parametrize("entry", ["normalizer", "http_rebuild"])
@pytest.mark.parametrize(
    "override",
    [
        {"tr": "H0GSCNI0"},
        {"tr": "H0GSCNI9"},
        {"tr": "UNKNOWN"},
        {"fields": []},
        {"fields": "not-a-field-list"},
        {"fields": [None] * 26},
        {"received_at": "bad"},
        {"received_at": "2026-11-02T09:30:01"},
        {"received_at": "9999-12-31T23:59:59Z"},
    ],
)
def test_invalid_fillwire_envelope_raises_instead_of_unknown_or_canonical_fallback(
    entry: str,
    override: dict,
) -> None:
    raw = fillwire_kis_domestic_frame(**override)
    with pytest.raises(ValueError, match="KIS fillwire"):
        if entry == "normalizer":
            normalize_kis_fill(raw)
        else:
            _rebuild_fill_order(_fill(raw))


@pytest.mark.parametrize("entry", ["normalizer", "http_rebuild"])
@pytest.mark.parametrize(
    "index,value",
    [
        (2, ""),
        (4, "?"),
        (8, "UNKNOWN"),
        (9, "0"),
        (9, "1.5"),
        (10, "NaN"),
        (10, "Infinity"),
        (10, "-1"),
        (11, "246000"),
        (12, "1"),
        (13, "1"),
        (14, "3"),
        (5, "2"),
    ],
)
def test_nonfill_or_invalid_domestic_values_raise(
    entry: str, index: int, value: str
) -> None:
    raw = fillwire_kis_domestic_frame()
    raw["fields"][index] = value
    with pytest.raises(ValueError, match="KIS fillwire"):
        if entry == "normalizer":
            normalize_kis_fill(raw)
        else:
            _rebuild_fill_order(_fill(raw))


def test_domestic_clock_uses_received_kst_day_even_on_delayed_delivery() -> None:
    raw = fillwire_kis_domestic_frame(received_at="2026-11-02T15:00:01Z")
    raw["fields"][11] = "235959"
    order = normalize_kis_fill(raw)
    assert datetime.fromisoformat(order.filled_at) == datetime(
        2026, 11, 2, 14, 59, 59, tzinfo=UTC
    )


def test_padded_domestic_fill_codes_match_fillwire_classification() -> None:
    raw = fillwire_kis_domestic_frame()
    for index, value in {12: " 00 ", 5: "01", 14: "02", 13: "002"}.items():
        raw["fields"][index] = value
    assert normalize_kis_fill(raw).fill_status == "filled"


@pytest.mark.parametrize("key", ["tr", "fields", "received_at"])
def test_incomplete_fillwire_frame_cannot_fall_back_to_canonical(key: str) -> None:
    raw = fillwire_kis_domestic_frame()
    del raw[key]
    with pytest.raises(ValueError, match="KIS fillwire"):
        _rebuild_fill_order(_fill(raw))


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "000660"),
        ("side", "sell"),
        ("filled_qty", Decimal("11")),
        ("filled_price", Decimal("70001")),
        ("filled_price", Decimal("70000.000000000001")),
        ("broker_order_id", "0000123457"),
        ("instrument_type", "equity_us"),
        ("currency", "USD"),
    ],
)
def test_frame_must_match_the_committed_canonical_fill(
    field: str, value: object
) -> None:
    fill = _fill(fillwire_kis_domestic_frame())
    setattr(fill, field, value)
    with pytest.raises(ValueError, match="canonical"):
        _rebuild_fill_order(fill)


@pytest.mark.parametrize("account_mode,expected", [("live", "kis"), ("mock", "mock")])
def test_fillwire_account_label_comes_from_the_canonical_row(
    account_mode: str, expected: str
) -> None:
    raw = fillwire_kis_domestic_frame(
        tr="H0STCNI9" if account_mode == "mock" else "H0STCNI0"
    )
    fill = _fill(raw)
    fill.account_mode = account_mode
    assert _rebuild_fill_order(fill).account == expected


@pytest.mark.parametrize("delta_seconds", [1, 1800, 3600])
def test_any_future_fill_clock_is_assigned_to_the_previous_day(
    delta_seconds: int,
) -> None:
    raw = fillwire_kis_domestic_frame(received_at="2026-11-02T13:00:00Z")
    clock = datetime(2026, 11, 2, 22) + timedelta(seconds=delta_seconds)
    raw["fields"][11] = clock.strftime("%H%M%S")
    order = normalize_kis_fill(raw)
    assert datetime.fromisoformat(order.filled_at) == datetime(
        2026, 11, 1, 13, tzinfo=UTC
    ) + timedelta(seconds=delta_seconds)


def test_krx_alphanumeric_short_code_notifies_with_canonical_identity() -> None:
    raw = fillwire_kis_domestic_frame()
    raw["fields"][8] = "0074K0"
    fill = _fill(raw)
    fill.symbol = fill.raw_symbol = "0074K0"
    order = _rebuild_fill_order(fill)
    assert order.symbol == "0074K0"
    assert order.market_type == "kr"
    assert is_fill_notifiable(order)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [False, True])
async def test_route_keeps_commit_and_reports_raw_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mismatch: bool
) -> None:
    raw = fillwire_kis_domestic_frame()
    if mismatch:
        raw["fields"][8] = "000660"
    fill = _fill(raw)
    repository = MagicMock()
    repository.upsert_fill = AsyncMock(return_value=("inserted", 123))
    monkeypatch.setattr(
        ingest_module, "ExecutionLedgerRepository", lambda db: repository
    )
    db = MagicMock()
    db.begin_nested.return_value.__aenter__ = AsyncMock()
    db.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    db.commit = AsyncMock()
    downstream = AsyncMock()
    monkeypatch.setattr(ingest_module, "run_post_upsert_downstream", downstream)

    response = await ingest_module.ingest_execution_ledger_fills(
        ExecutionLedgerFillIngestRequest(
            fills=[fill.model_dump(mode="json")], source="fillwire"
        ),
        db,
    )

    assert response.accepted == 1
    assert response.results[0].status == "inserted"
    assert response.results[0].row_id == 123
    db.commit.assert_awaited_once()
    db.rollback.assert_not_called()
    assert repository.upsert_fill.call_args.args[0].symbol == "005930"
    if mismatch:
        downstream.assert_not_awaited()
        assert "downstream failed after commit" in caplog.text
        assert "disagrees with canonical fill" in caplog.text
    else:
        downstream.assert_awaited_once()
        assert downstream.call_args.kwargs["fill_order"].symbol == "005930"
