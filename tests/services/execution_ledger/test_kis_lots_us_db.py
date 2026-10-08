"""Task #1173 — DB-backed tests for the KIS live US lot block (test DB only).

Every test runs with the KIS client constructor and every httpx send trapped
and asserts neither was reached: the US loader and the get_holdings opt-in are
DB-only. Timestamps sit in 2099-08 so this file's reconcile runs are the latest
kis run in the test database while a test runs; every row uses a T1173 symbol
or order number and is deleted afterwards. The quarantined-row proof (with its
"filter dropped" mutant) lives in test_quarantine_readers_db.py next to the KR
reader proof.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.models.execution_ledger import ExecutionLedger, ExecutionLedgerReconcileRun
from app.models.review import KISLiveOrderLedger, LiveOrderLedger
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.brokers.kis.base import BaseKISClient
from app.services.execution_ledger import kis_lots
from app.services.execution_ledger.kis_lots import (
    PositionRef,
    load_kis_live_kr_lot_blocks,
    load_kis_live_us_lot_blocks,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# 2099-08-12 10:00 EDT = 23:00 KST, inside the US regular session
NOW = datetime(2099, 8, 12, 14, 0, tzinfo=UTC)
SESSION = datetime(2099, 8, 12, 13, 45, tzinfo=UTC)  # 09:45 EDT
LATE = datetime(2099, 8, 12, 16, 0, tzinfo=UTC)  # 01:00 KST, same US date
SYM = "T1173A"
DOT = "T1173.B"  # DB dot-format; the reconciler stores KIS pdno "T1173/B"
ORDER_PREFIX = "T1173"
ALL_SYMBOLS = [
    "T1173A",
    "T1173C",
    "T1173D",
    "T1173E",
    "T1173.B",
    "T1173/B",
    "T1173-B",
    "T1173.C",
    "T1173/C",
    "t1173a",
    " T1173A ",
    "t1173/b",
]
MIXED_KEY = "T1173.X.Y"
# r3 F3 / round 4: spellings _us_symbol_key maps to MIXED_KEY ...
MIXED_SAME = [
    "t1173/x-y",
    "T1173.X/Y",
    "T1173-X.Y",
    "t1173-x-y",
    "T1173/X/Y",
    "\tt1173.x.y ",
]
# ... and near misses it does not (must not be merged into the position).
MIXED_OTHER = ["T1173_X.Y", "T1173 X.Y", "T1173.XY", "T1173.X.Y.Z", "T1173XY"]
ALL_SYMBOLS += [MIXED_KEY, *MIXED_SAME, *MIXED_OTHER]


@pytest.fixture(autouse=True)
def no_kis_or_http(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Trap KIS client construction and every outbound httpx send."""
    calls: list[str] = []

    def kis_init(self: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(f"kis:{type(self).__name__}")
        raise AssertionError("KIS client constructed")

    async def async_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(f"http:{request.url}")
        raise AssertionError("outbound HTTP")

    def sync_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(f"http:{request.url}")
        raise AssertionError("outbound HTTP")

    monkeypatch.setattr(BaseKISClient, "__init__", kis_init)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    monkeypatch.setattr(httpx.Client, "send", sync_send)
    yield calls
    assert calls == [], calls


def _fill(**overrides: Any) -> ExecutionLedger:
    data: dict[str, Any] = {
        "broker": "kis",
        "account_mode": "live",
        "venue": "NASD",
        "instrument_type": "equity_us",
        "symbol": SYM,
        "raw_symbol": SYM,
        "side": "buy",
        "broker_order_id": f"{ORDER_PREFIX}-{uuid.uuid4().hex[:8]}",
        "fill_seq": 0,
        "filled_qty": Decimal("4"),
        "filled_price": Decimal("400"),
        "filled_at": NOW - timedelta(days=6),
        "currency": "USD",
        "source": "reconciler",
    }
    data.update(overrides)
    if "symbol" in overrides and "raw_symbol" not in overrides:
        data["raw_symbol"] = overrides["symbol"]
    return ExecutionLedger(**ExecutionLedgerUpsert(**data).model_dump())


def _seed(symbol: str = SYM, qty: str = "5", **overrides: Any) -> ExecutionLedger:
    return _fill(
        symbol=symbol,
        source="manual_import",
        broker_order_id=f"SEED-20990701-{ORDER_PREFIX}-{uuid.uuid4().hex[:6]}",
        filled_qty=Decimal(qty),
        filled_price=Decimal("380"),
        filled_at=datetime(2099, 7, 1, tzinfo=UTC),
        **overrides,
    )


def _live_order(order_no: str, **overrides: Any) -> LiveOrderLedger:
    data: dict[str, Any] = {
        "trade_date": SESSION,
        "broker": "kis",
        "account_scope": "kis_live",
        "market": "us",
        "symbol": SYM,
        "exchange": "NASD",
        "side": "sell",
        "order_kind": "limit",
        "quantity": Decimal("3"),
        "price": Decimal("430"),
        "currency": "USD",
        "order_no": order_no,
        "status": "accepted",
        "lifecycle_state": "accepted",
    }
    data.update(overrides)
    return LiveOrderLedger(**data)


def _kis_order(order_no: str, **overrides: Any) -> KISLiveOrderLedger:
    data: dict[str, Any] = {
        "trade_date": SESSION,
        "symbol": SYM,
        "instrument_type": "equity_us",
        "side": "buy",
        "order_type": "limit",
        "quantity": Decimal("1"),
        "price": Decimal("390"),
        "order_no": order_no,
        "account_mode": "kis_live",
        "broker": "kis",
        "status": "accepted",
        "lifecycle_state": "accepted",
    }
    data.update(overrides)
    return KISLiveOrderLedger(**data)


def _run(finished_ago: timedelta, *, now: datetime = NOW):
    started = now - finished_ago - timedelta(minutes=1)
    return ExecutionLedgerReconcileRun(
        run_id=uuid.uuid4(),
        broker="kis",
        window_start=started - timedelta(hours=24),
        window_end=started,
        started_at=started,
        finished_at=now - finished_ago,
        dry_run=False,
    )


async def _cleanup(db_session, run_ids: list[uuid.UUID]) -> None:
    await db_session.rollback()
    await db_session.execute(
        delete(ExecutionLedger).where(ExecutionLedger.symbol.in_(ALL_SYMBOLS))
    )
    await db_session.execute(
        delete(LiveOrderLedger).where(LiveOrderLedger.order_no.like(f"{ORDER_PREFIX}%"))
    )
    await db_session.execute(
        delete(KISLiveOrderLedger).where(
            KISLiveOrderLedger.order_no.like(f"{ORDER_PREFIX}%")
        )
    )
    await db_session.execute(
        delete(ExecutionLedgerReconcileRun).where(
            ExecutionLedgerReconcileRun.run_id.in_(run_ids)
        )
    )
    await db_session.commit()


async def _load(db_session, rows: list[Any], refs, *, now: datetime = NOW):
    run_ids = [r.run_id for r in rows if isinstance(r, ExecutionLedgerReconcileRun)]
    db_session.add_all(rows)
    await db_session.commit()
    try:
        return await load_kis_live_us_lot_blocks(db_session, refs, now=now)
    finally:
        await _cleanup(db_session, run_ids)


# ------------------------------------------------------------------ A1


async def test_a1_us_loader_known_block_with_decoys_excluded(db_session) -> None:
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10)),
            _seed(),
            _fill(venue="NYSE"),
            _fill(
                side="sell",
                filled_qty=Decimal("1"),
                filled_price=Decimal("410"),
                filled_at=NOW - timedelta(days=3),
                venue="AMEX",
            ),
            # decoys: none of these is a KIS live US fill of SYM
            _fill(instrument_type="equity_kr", currency="KRW", venue="krx"),
            _fill(account_mode="mock"),
            _fill(currency="KRW"),
            _fill(symbol="T1173C"),
        ],
        [PositionRef(SYM, Decimal("8"), Decimal("420"))],
    )
    block = blocks[SYM]
    assert block["ledger_state"] == "known", block["unknown_reasons"]
    assert block["net_quantity"] == "8"
    assert [(lot["quantity"], lot["unit_cost"]) for lot in block["lots"]] == [
        ("4", "380.0000"),
        ("4", "400.0000"),
    ]
    assert block["freshness"]["state"] == "fresh"
    assert block["market"] == "us"
    assert block["open_buy_evidence"]["external_orders_verifiable"] is False
    assert block["sellable_by_ledger"] == "8"


async def test_a1_us_quantity_mismatch_is_unknown(db_session) -> None:
    blocks = await _load(
        db_session,
        [_run(timedelta(minutes=10)), _seed(), _fill(venue="NYSE")],
        [PositionRef(SYM, Decimal("10"), Decimal("420"))],
    )
    block = blocks[SYM]
    assert block["ledger_state"] == "unknown"
    assert block["unknown_reasons"] == ["quantity_mismatch_with_reference"]
    assert block["lots"] is None
    assert block["sellable_by_ledger"] is None


async def test_a1_stale_reconcile_is_unknown(db_session) -> None:
    stale = await _load(
        db_session,
        [_run(timedelta(minutes=95)), _seed(qty="8")],
        [PositionRef(SYM, Decimal("8"))],
    )
    assert stale[SYM]["unknown_reasons"] == ["ledger_stale"]


async def test_db_symbol_spellings_map_to_the_holdings_symbol(db_session) -> None:
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10)),
            _seed(symbol="T1173/B", qty="2"),
            _fill(symbol="T1173/B", filled_qty=Decimal("1")),
            _fill(symbol="T1173.B", filled_qty=Decimal("1")),
            # provisional websocket spelling: listed, never counted
            _fill(
                symbol="T1173-B",
                source="websocket",
                filled_at=SESSION,
                venue="krx",
            ),
            # a different symbol with a similar spelling stays out
            _fill(symbol="T1173/C", filled_qty=Decimal("9")),
        ],
        [PositionRef(DOT, Decimal("4"))],
    )
    block = blocks[DOT]
    assert block["ledger_state"] == "known", block["unknown_reasons"]
    assert block["net_quantity"] == "4"
    assert len(block["provisional_rows_excluded"]) == 1
    assert block["same_day_buy_evidence"]["blocking"] is True


async def test_unrecognized_venue_row_turns_the_block_unknown(db_session) -> None:
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10)),
            _seed(),
            _fill(venue="NASDAQ", filled_qty=Decimal("3")),
        ],
        [PositionRef(SYM, Decimal("8"))],
    )
    block = blocks[SYM]
    assert block["ledger_state"] == "unknown"
    assert block["unknown_reasons"] == [
        "unrecognized_us_venue_rows",
        "quantity_mismatch_with_reference",
    ]
    [row] = block["diagnostics"]["unrecognized_venue_rows"]
    assert row["venue"] == "NASDAQ"


# ------------------------------------------------------------------ A2


async def test_a2_unquarantined_phantom_websocket_row_never_counts(db_session) -> None:
    # A websocket buy recorded from an accept notice (filled 0 at the broker).
    phantom = _fill(
        source="websocket",
        filled_qty=Decimal("2"),
        filled_at=SESSION,
        raw_payload_json={"tr": "H0GSCNI0", "cntg_yn": "1"},
    )
    blocks = await _load(
        db_session,
        [_run(timedelta(minutes=10)), _seed(qty="8"), phantom],
        [PositionRef(SYM, Decimal("10"))],
    )
    block = blocks[SYM]
    assert block["ledger_state"] == "unknown"
    assert block["unknown_reasons"] == [
        "quantity_mismatch_with_reference",
        "provisional_rows_pending_reconcile",
    ]
    assert block["lots"] is None
    assert block["sellable_by_ledger"] is None


async def test_a2_loader_keeps_today_fills_that_reuse_old_order_numbers(
    db_session,
) -> None:
    """Tester r1 F1 through the real loader: recurring KIS order numbers."""
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10)),
            _fill(filled_qty=Decimal("10"), broker_order_id="000123"),
            _fill(
                side="sell",
                filled_qty=Decimal("2"),
                broker_order_id="000456",
                filled_at=NOW - timedelta(days=2),
            ),
            # 23:30 KST and 00:30 KST, one US trading date, reused numbers
            _fill(
                source="websocket",
                filled_qty=Decimal("1"),
                broker_order_id="123",
                filled_at=datetime(2099, 8, 12, 14, 30, tzinfo=UTC),
            ),
            _fill(
                source="websocket",
                side="sell",
                filled_qty=Decimal("1"),
                broker_order_id="456",
                filled_at=datetime(2099, 8, 12, 15, 30, tzinfo=UTC),
            ),
        ],
        [PositionRef(SYM, Decimal("8"))],
        now=LATE,
    )
    block = blocks[SYM]
    assert block["diagnostics"]["superseded_websocket_duplicates"] == 0
    for key in (
        "open_buy_evidence",
        "same_day_sell_evidence",
        "open_sell_evidence",
        "same_day_buy_evidence",
    ):
        assert block[key]["blocking"] is True, key


async def test_a2_symbol_case_and_whitespace_cannot_hide_today_fills(
    db_session,
) -> None:
    """Tester r2 F2: the ingest schema accepts any symbol case.

    A lowercase or padded spelling must reach the same position in SQL, so a
    net-zero buy/sell pair of today still blocks every evidence view.
    """
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10), now=LATE),
            _fill(filled_qty=Decimal("8")),
            _fill(
                symbol="t1173a",
                source="websocket",
                filled_qty=Decimal("1"),
                filled_at=datetime(2099, 8, 12, 14, 30, tzinfo=UTC),
            ),
            _fill(
                symbol=" T1173A ",
                source="websocket",
                side="sell",
                filled_qty=Decimal("1"),
                filled_at=datetime(2099, 8, 12, 15, 30, tzinfo=UTC),
            ),
        ],
        [PositionRef(SYM, Decimal("8"))],
        now=LATE,
    )
    block = blocks[SYM]
    assert block["freshness"]["state"] == "fresh"
    assert block["ledger_state"] == "known", block["unknown_reasons"]
    assert len(block["provisional_rows_excluded"]) == 2
    for key in (
        "open_buy_evidence",
        "same_day_sell_evidence",
        "open_sell_evidence",
        "same_day_buy_evidence",
    ):
        assert block[key]["blocking"] is True, key


async def test_symbol_case_reaches_authoritative_rows_and_both_order_ledgers(
    db_session,
) -> None:
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10)),
            _seed(symbol="t1173/b", qty="5"),
            _live_order(f"{ORDER_PREFIX}61", symbol="t1173.b", quantity=Decimal("2")),
            _kis_order(f"{ORDER_PREFIX}62", symbol=" T1173.B "),
        ],
        [PositionRef(DOT, Decimal("5"))],
    )
    block = blocks[DOT]
    assert block["ledger_state"] == "known", block["unknown_reasons"]
    assert block["net_quantity"] == "5"
    assert block["open_sell_evidence"]["own_open_sell_order_quantity"] == "2"
    assert block["sellable_by_ledger"] == "3"
    assert [
        o["order_no"]
        for o in block["open_buy_evidence"]["kis_live_order_ledger_open_buys"]
    ] == [f"{ORDER_PREFIX}62"]


async def test_a2_mixed_separator_spellings_cannot_hide_today_fills(
    db_session,
) -> None:
    """Tester r3 F3: to_db_symbol maps every / and - to a dot.

    A stored r3pref/a-b style spelling of the held key must pass the SQL
    prefilter; an authoritative mixed row counts toward the lots, and a
    net-zero websocket buy/sell pair of today blocks every evidence view.
    """
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10), now=LATE),
            _fill(symbol="T1173/X-Y", filled_qty=Decimal("5")),
            _fill(symbol="t1173-x.y", filled_qty=Decimal("3")),
            _fill(
                symbol="t1173/x-y",
                source="websocket",
                filled_qty=Decimal("1"),
                filled_at=datetime(2099, 8, 12, 14, 30, tzinfo=UTC),
            ),
            _fill(
                symbol="T1173.X/Y",
                source="websocket",
                side="sell",
                filled_qty=Decimal("1"),
                filled_at=datetime(2099, 8, 12, 15, 30, tzinfo=UTC),
            ),
        ],
        [PositionRef(MIXED_KEY, Decimal("8"))],
        now=LATE,
    )
    block = blocks[MIXED_KEY]
    assert block["freshness"]["state"] == "fresh"
    assert block["ledger_state"] == "known", block["unknown_reasons"]
    assert block["net_quantity"] == "8"
    assert len(block["provisional_rows_excluded"]) == 2
    for key in (
        "open_buy_evidence",
        "same_day_sell_evidence",
        "open_sell_evidence",
        "same_day_buy_evidence",
    ):
        assert block[key]["blocking"] is True, key


async def test_sql_prefilter_selects_exactly_what_the_python_key_maps(
    db_session,
) -> None:
    """The SQL identity and _us_symbol_key agree on every stored spelling.

    Selected rows must be exactly the ones whose Python key is the position
    key: nothing the mapping would attribute is dropped (fail-open), and no
    near-miss spelling is merged into the position.
    """
    spellings = [*MIXED_SAME, *MIXED_OTHER]
    rows = [
        _fill(symbol=sym, broker_order_id=f"{ORDER_PREFIX}-M{i:02d}")
        for i, sym in enumerate(spellings)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    try:
        selected = set(
            (
                await db_session.execute(
                    select(ExecutionLedger.symbol)
                    .where(ExecutionLedger.broker_order_id.like(f"{ORDER_PREFIX}-M%"))
                    .where(
                        kis_lots._symbol_matches(ExecutionLedger.symbol, [MIXED_KEY])
                    )
                )
            )
            .scalars()
            .all()
        )
        stored = set(
            (
                await db_session.execute(
                    select(ExecutionLedger.symbol).where(
                        ExecutionLedger.broker_order_id.like(f"{ORDER_PREFIX}-M%")
                    )
                )
            )
            .scalars()
            .all()
        )
    finally:
        await _cleanup(db_session, [])
    expected = {sym for sym in stored if kis_lots._us_symbol_key(sym) == MIXED_KEY}
    # the write schema strips outer whitespace only; every spelling is stored
    assert stored == {sym.strip() for sym in spellings}
    assert expected == {sym.strip() for sym in MIXED_SAME}
    assert selected == expected


# ------------------------------------------------------------------ A3


async def test_a3_us_order_ledgers_feed_the_sell_side_evidence(db_session) -> None:
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10)),
            _seed(qty="8"),
            # counted: same-US-date non-terminal own sell (review.live_order_ledger)
            _live_order(f"{ORDER_PREFIX}01", quantity=Decimal("3")),
            # previous US date: reported as presumed dead, never reserved
            _live_order(f"{ORDER_PREFIX}02", trade_date=SESSION - timedelta(days=1)),
            # decoys
            _live_order(f"{ORDER_PREFIX}03", status="filled"),
            _live_order(
                f"{ORDER_PREFIX}04", broker="upbit", account_scope="upbit_live"
            ),
            _live_order(f"{ORDER_PREFIX}05", market="crypto"),
            _live_order(f"{ORDER_PREFIX}06", symbol="T1173C"),
            _live_order(f"{ORDER_PREFIX}07", trade_date=SESSION - timedelta(days=30)),
            # an equity_us row in the KR order ledger still counts (buy side)
            _kis_order(f"{ORDER_PREFIX}08"),
            _kis_order(f"{ORDER_PREFIX}09", instrument_type="equity_kr"),
            _kis_order(f"{ORDER_PREFIX}10", account_mode="kis_mock"),
        ],
        [PositionRef(SYM, Decimal("8"))],
    )
    block = blocks[SYM]
    assert block["ledger_state"] == "known", block["unknown_reasons"]
    sell = block["open_sell_evidence"]
    assert sell["state"] == "known"
    assert sell["blocking_reasons"] == ["own_nonterminal_sell_order_today"]
    assert [o["order_no"] for o in sell["kis_live_order_ledger_open_sells"]] == [
        f"{ORDER_PREFIX}01"
    ]
    assert [o["order_no"] for o in sell["presumed_dead_prior_day_sells"]] == [
        f"{ORDER_PREFIX}02"
    ]
    assert sell["own_open_sell_order_quantity"] == "3"
    assert block["sellable_by_ledger"] == "5"
    buy = block["open_buy_evidence"]
    assert [o["order_no"] for o in buy["kis_live_order_ledger_open_buys"]] == [
        f"{ORDER_PREFIX}08"
    ]
    assert buy["blocking"] is True


async def test_a3_us_order_read_failure_degrades_only_the_order_evidence(
    db_session,
) -> None:
    class OrderReadFails:
        def __init__(self, inner):
            self._inner = inner

        async def execute(self, statement, *args, **kwargs):
            if "live_order_ledger" in str(statement):
                raise RuntimeError("boom")
            return await self._inner.execute(statement, *args, **kwargs)

        async def rollback(self):
            await self._inner.rollback()

    run = _run(timedelta(minutes=10))
    run_ids = [run.run_id]
    db_session.add_all([run, _seed(qty="8")])
    await db_session.commit()
    try:
        blocks = await load_kis_live_us_lot_blocks(
            OrderReadFails(db_session),  # type: ignore[arg-type]
            [PositionRef(SYM, Decimal("8"))],
            now=NOW,
        )
    finally:
        await _cleanup(db_session, run_ids)
    block = blocks[SYM]
    assert block["ledger_state"] == "known"
    for key in ("open_buy_evidence", "open_sell_evidence"):
        assert block[key]["state"] == "unknown", key
        assert block[key]["unknown_reasons"] == ["order_ledger_read_failed"], key
        assert block[key]["blocking"] is True, key
    assert block["sellable_by_ledger"] is None


async def test_us_fills_read_failure_raises_for_the_caller_to_degrade() -> None:
    class Boom:
        async def execute(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await load_kis_live_us_lot_blocks(
            Boom(),  # type: ignore[arg-type]
            [PositionRef(SYM, Decimal("1"))],
            now=NOW,
        )
    assert await load_kis_live_us_lot_blocks(Boom(), [], now=NOW) == {}  # type: ignore[arg-type]


# ------------------------------------------------------------------ A4


async def test_a4_kr_loader_never_reads_us_rows(db_session) -> None:
    run = _run(timedelta(minutes=10))
    run_ids = [run.run_id]
    db_session.add_all([run, _seed(qty="8")])
    await db_session.commit()
    try:
        kr = await load_kis_live_kr_lot_blocks(
            db_session, [PositionRef(SYM, Decimal("8"))], now=NOW
        )
    finally:
        await _cleanup(db_session, run_ids)
    assert kr[SYM]["unknown_reasons"] == [
        "no_ledger_rows",
        "quantity_mismatch_with_reference",
    ]
    assert "market" not in kr[SYM]


async def test_get_holdings_opt_in_end_to_end_kr_and_us(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_holdings(include_ledger_lots=True) with the real loaders and session."""
    from tests.mcp_server import get_holdings_golden_support as support

    us_held, us_failing, kr_held = "T1173D", "T1173E", "T1173A"
    support.install_fake_collect(
        monkeypatch,
        [
            support.kis_kr_position(kr_held, 2.0, 1000.0),
            support.kis_us_position(us_held),
            support.kis_us_position(us_failing),
        ],
    )
    now = datetime.now(UTC)
    run = _run(timedelta(minutes=10), now=now)
    run_ids = [run.run_id]
    db_session.add_all(
        [
            run,
            _fill(
                symbol=us_held,
                filled_qty=Decimal("3"),
                filled_at=now - timedelta(days=40),
            ),
            _live_order(
                f"{ORDER_PREFIX}41",
                symbol=us_held,
                trade_date=now,
                quantity=Decimal("1"),
            ),
            _fill(
                symbol=kr_held,
                instrument_type="equity_kr",
                currency="KRW",
                venue="krx",
                filled_qty=Decimal("2"),
                filled_at=now - timedelta(days=40),
            ),
        ]
    )
    await db_session.commit()
    try:
        result = await support.call_get_holdings(
            include_current_price=False, minimum_value=0, include_ledger_lots=True
        )
    finally:
        await _cleanup(db_session, run_ids)

    blocks = {
        (p["market"], p["symbol"]): p["ledger_lots"]
        for g in result["accounts"]
        for p in g["positions"]
        if "ledger_lots" in p
    }
    us_block = blocks[("us", us_held)]
    assert us_block["ledger_state"] == "known", us_block["unknown_reasons"]
    assert us_block["open_sell_evidence"]["blocking_reasons"] == [
        "own_nonterminal_sell_order_today"
    ]
    assert us_block["sellable_by_ledger"] == "2"
    assert blocks[("us", us_failing)]["ledger_state"] == "unknown"
    kr_block = blocks[("kr", kr_held)]
    assert kr_block["ledger_state"] == "known", kr_block["unknown_reasons"]
    assert "market" not in kr_block
    assert result["ledger_lots"]["positions_covered_by_market"] == {"kr": 1, "us": 2}


async def test_the_kis_and_http_trap_is_live(no_kis_or_http: list[str]) -> None:
    from app.services.brokers.kis.client import KISClient

    with pytest.raises(AssertionError):
        KISClient()
    async with httpx.AsyncClient() as client:
        with pytest.raises(AssertionError):
            await client.get("http://127.0.0.1:9/never")
    assert no_kis_or_http == ["kis:KISClient", "http:http://127.0.0.1:9/never"]
    no_kis_or_http.clear()


async def test_zz_this_file_leaves_no_rows_behind(db_session) -> None:
    await db_session.rollback()
    counts = []
    for model, column, pattern in (
        (ExecutionLedger, ExecutionLedger.symbol, None),
        (LiveOrderLedger, LiveOrderLedger.order_no, f"{ORDER_PREFIX}%"),
        (KISLiveOrderLedger, KISLiveOrderLedger.order_no, f"{ORDER_PREFIX}%"),
    ):
        where = column.in_(ALL_SYMBOLS) if pattern is None else column.like(pattern)
        counts.append(
            (
                await db_session.execute(
                    select(func.count()).select_from(model).where(where)
                )
            ).scalar_one()
        )
    assert counts == [0, 0, 0]
