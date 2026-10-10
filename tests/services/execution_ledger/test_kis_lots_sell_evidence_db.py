"""Task #1087 — DB-backed tests for the KIS sell-side ledger evidence (test DB only).

Every test runs with the KIS client constructor and every httpx send trapped,
and asserts that neither was reached: the loader and the get_holdings opt-in
are DB-only. Timestamps sit in 2099-07 so this file's reconcile runs are the
latest kis run in the test database; every row uses a T1087 symbol or order
number and is deleted afterwards.
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
from app.models.review import KISLiveOrderLedger
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.brokers.kis.base import BaseKISClient
from app.services.execution_ledger.kis_lots import (
    PositionRef,
    load_kis_live_kr_lot_blocks,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# 2099-07-15 14:30 KST
NOW = datetime(2099, 7, 15, 5, 30, tzinfo=UTC)
# 2099-07-15 00:00 KST, the start of NOW's KST day
KST_MIDNIGHT = datetime(2099, 7, 14, 15, 0, tzinfo=UTC)
SYM = "T108701"
ORDER_PREFIX = "T1087"
ALL_SYMBOLS = [f"T1087{i:02d}" for i in range(1, 12)]


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
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": SYM,
        "raw_symbol": SYM,
        "side": "buy",
        "broker_order_id": f"{ORDER_PREFIX}-{uuid.uuid4().hex[:8]}",
        "fill_seq": 0,
        "filled_qty": Decimal("12"),
        "filled_price": Decimal("1000"),
        "filled_at": NOW - timedelta(days=5),
        "currency": "KRW",
        "source": "reconciler",
    }
    data.update(overrides)
    if "symbol" in overrides and "raw_symbol" not in overrides:
        data["raw_symbol"] = overrides["symbol"]
    return ExecutionLedger(**ExecutionLedgerUpsert(**data).model_dump())


def _order(order_no: str, **overrides: Any) -> KISLiveOrderLedger:
    data: dict[str, Any] = {
        "trade_date": NOW - timedelta(hours=4),
        "symbol": SYM,
        "instrument_type": "equity_kr",
        "side": "sell",
        "order_type": "limit",
        "quantity": Decimal("5"),
        "price": Decimal("1000"),
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
        return await load_kis_live_kr_lot_blocks(db_session, refs, now=now)
    finally:
        await _cleanup(db_session, run_ids)


async def test_loader_reads_own_sell_orders_and_reserves_their_quantity(
    db_session,
) -> None:
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10)),
            _fill(),
            # counted: same-day non-terminal own sell
            _order(f"{ORDER_PREFIX}01", status="accepted", quantity=Decimal("5")),
            # prior-day non-terminal sell: reported, not blocking, not reserved
            _order(
                f"{ORDER_PREFIX}02",
                status="accepted",
                trade_date=NOW - timedelta(days=1),
            ),
            # decoys that must not leak into the sell view
            _order(f"{ORDER_PREFIX}03", side="buy", quantity=Decimal("50")),
            _order(
                f"{ORDER_PREFIX}04", account_mode="kis_mock", quantity=Decimal("50")
            ),
            _order(f"{ORDER_PREFIX}05", symbol="T108702", quantity=Decimal("50")),
            _order(f"{ORDER_PREFIX}06", status="filled", quantity=Decimal("50")),
            _order(f"{ORDER_PREFIX}07", broker="toss", quantity=Decimal("50")),
            _order(
                f"{ORDER_PREFIX}08",
                trade_date=NOW - timedelta(days=30),
                quantity=Decimal("50"),
            ),
        ],
        [PositionRef(SYM, Decimal("12"), Decimal("1100"))],
    )
    block = blocks[SYM]
    assert block["ledger_state"] == "known", block["unknown_reasons"]
    evidence = block["open_sell_evidence"]
    assert evidence["state"] == "known"
    assert evidence["blocking_reasons"] == ["own_nonterminal_sell_order_today"]
    assert [o["order_no"] for o in evidence["kis_live_order_ledger_open_sells"]] == [
        f"{ORDER_PREFIX}01"
    ]
    assert [o["order_no"] for o in evidence["presumed_dead_prior_day_sells"]] == [
        f"{ORDER_PREFIX}02"
    ]
    assert evidence["own_open_sell_order_quantity"] == "5"
    assert evidence["external_orders_verifiable"] is False
    assert block["sellable_by_ledger"] == "7"
    # the buy decoy lands on the buy side only
    assert [
        o["order_no"]
        for o in block["open_buy_evidence"]["kis_live_order_ledger_open_buys"]
    ] == [f"{ORDER_PREFIX}03"]


async def test_over_reserved_sells_clamp_sellable_at_zero(db_session) -> None:
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10)),
            _fill(),
            _order(f"{ORDER_PREFIX}11", status="partial", quantity=Decimal("8")),
            _order(f"{ORDER_PREFIX}12", status="pending", quantity=Decimal("9")),
        ],
        [PositionRef(SYM, Decimal("12"))],
    )
    block = blocks[SYM]
    assert block["sellable_by_ledger"] == "0"
    assert block["sellable_by_ledger_basis"]["clamped"] is True
    assert block["open_sell_evidence"]["own_open_sell_order_quantity"] == "17"


@pytest.mark.parametrize(
    ("finished_ago", "state"),
    [
        (timedelta(minutes=90), "known"),
        (timedelta(minutes=90, seconds=1), "unknown"),
    ],
)
async def test_staleness_edge_through_the_loader(
    db_session, finished_ago: timedelta, state: str
) -> None:
    blocks = await _load(
        db_session,
        [
            _run(finished_ago),
            _fill(),
            _fill(
                side="sell",
                filled_qty=Decimal("2"),
                filled_at=NOW - timedelta(hours=1),
                broker_order_id="1087901",
            ),
            _fill(
                filled_qty=Decimal("1"),
                filled_at=NOW - timedelta(hours=2),
                broker_order_id="1087902",
            ),
            _order(f"{ORDER_PREFIX}21", status="accepted", quantity=Decimal("3")),
        ],
        [PositionRef(SYM, Decimal("11"))],
    )
    block = blocks[SYM]
    sell = block["open_sell_evidence"]
    buy = block["same_day_buy_evidence"]
    concrete_sell = [
        "own_nonterminal_sell_order_today",
        "same_day_sell_fill_order_not_proven_complete",
    ]
    assert sell["state"] == state
    assert buy["state"] == state
    assert sell["blocking"] is True
    assert buy["blocking"] is True
    if state == "known":
        assert sell["blocking_reasons"] == concrete_sell
        assert buy["blocking_reasons"] == ["same_day_buy_fill_in_ledger"]
        assert block["sellable_by_ledger"] == "8"
    else:
        assert sell["unknown_reasons"] == ["ledger_stale"]
        assert sell["blocking_reasons"] == [
            "open_sell_evidence_unknown",
            *concrete_sell,
        ]
        assert buy["blocking_reasons"] == [
            "same_day_buy_evidence_unknown",
            "same_day_buy_fill_in_ledger",
        ]
        assert block["ledger_state"] == "unknown"
        assert block["sellable_by_ledger"] is None


async def test_no_committed_run_is_never_known(db_session) -> None:
    dry = _run(timedelta(minutes=5))
    dry.dry_run = True
    blocks = await _load(db_session, [dry, _fill()], [PositionRef(SYM, Decimal("12"))])
    block = blocks[SYM]
    assert block["open_sell_evidence"]["state"] == "unknown"
    assert block["open_sell_evidence"]["blocking"] is True
    assert block["same_day_buy_evidence"]["state"] == "unknown"
    assert block["sellable_by_ledger"] is None


async def test_kst_midnight_edge_through_the_loader(db_session) -> None:
    now = KST_MIDNIGHT + timedelta(seconds=30)  # 00:00:30 KST
    before = KST_MIDNIGHT - timedelta(seconds=1)  # 23:59:59 KST, previous day
    today_sym, prior_sym = "T108703", "T108704"
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=5), now=now),
            _fill(symbol=today_sym, filled_at=now - timedelta(days=5)),
            _fill(
                symbol=today_sym,
                side="sell",
                filled_qty=Decimal("2"),
                filled_at=KST_MIDNIGHT,
                broker_order_id="1087911",
            ),
            _fill(
                symbol=today_sym,
                filled_qty=Decimal("1"),
                filled_at=KST_MIDNIGHT,
                broker_order_id="1087912",
            ),
            _order(f"{ORDER_PREFIX}31", symbol=today_sym, trade_date=KST_MIDNIGHT),
            _fill(symbol=prior_sym, filled_at=now - timedelta(days=5)),
            _fill(
                symbol=prior_sym,
                side="sell",
                filled_qty=Decimal("2"),
                filled_at=before,
                broker_order_id="1087913",
            ),
            _fill(
                symbol=prior_sym,
                filled_qty=Decimal("1"),
                filled_at=before,
                broker_order_id="1087914",
            ),
            _order(f"{ORDER_PREFIX}32", symbol=prior_sym, trade_date=before),
        ],
        [PositionRef(today_sym, Decimal("11")), PositionRef(prior_sym, Decimal("11"))],
        now=now,
    )
    today = blocks[today_sym]
    assert today["open_sell_evidence"]["blocking_reasons"] == [
        "own_nonterminal_sell_order_today",
        "same_day_sell_fill_order_not_proven_complete",
    ]
    assert today["same_day_buy_evidence"]["blocking_reasons"] == [
        "same_day_buy_fill_in_ledger"
    ]
    assert today["sellable_by_ledger"] == "6"

    prior = blocks[prior_sym]
    assert prior["open_sell_evidence"]["blocking"] is False
    assert [
        o["order_no"]
        for o in prior["open_sell_evidence"]["presumed_dead_prior_day_sells"]
    ] == [f"{ORDER_PREFIX}32"]
    assert prior["same_day_buy_evidence"]["blocking"] is False
    assert prior["sellable_by_ledger"] == "11"


async def test_provisional_websocket_rows_block_but_never_count(db_session) -> None:
    ws_sell, ws_buy = "T108705", "T108706"
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=10)),
            _fill(symbol=ws_sell),
            _fill(
                symbol=ws_sell,
                side="sell",
                filled_qty=Decimal("2"),
                filled_at=NOW - timedelta(hours=1),
                broker_order_id="1087921",
                source="websocket",
                fill_seq=3,
            ),
            _fill(symbol=ws_buy),
            _fill(
                symbol=ws_buy,
                filled_qty=Decimal("3"),
                filled_at=NOW - timedelta(hours=1),
                broker_order_id="1087922",
                source="websocket",
                fill_seq=4,
            ),
        ],
        # the broker already reflects the websocket-only fills
        [PositionRef(ws_sell, Decimal("10")), PositionRef(ws_buy, Decimal("15"))],
    )
    sold = blocks[ws_sell]
    assert sold["open_sell_evidence"]["blocking_reasons"] == [
        "same_day_sell_fill_order_not_proven_complete"
    ]
    assert [
        f["provisional"]
        for f in sold["open_sell_evidence"]["same_day_sell_fills_unproven_complete"]
    ] == [True]
    assert sold["ledger_state"] == "unknown"
    assert sold["sellable_by_ledger"] is None

    bought = blocks[ws_buy]
    assert [f["provisional"] for f in bought["same_day_buy_evidence"]["fills"]] == [
        True
    ]
    assert bought["ledger_state"] == "unknown"
    assert bought["sellable_by_ledger"] is None  # never 15, never 12


@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_nonseed_manual_import_fill_today_is_evidence_through_the_loader(
    db_session, side: str
) -> None:
    """Round 1 BLOCKER: a non-SEED manual_import row is an actual fill."""
    sym = "T108709" if side == "buy" else "T108710"
    blocks = await _load(
        db_session,
        [
            _run(timedelta(minutes=90)),
            _fill(symbol=sym),
            _fill(
                symbol=sym,
                side=side,
                filled_qty=Decimal("2"),
                filled_at=KST_MIDNIGHT,
                broker_order_id="MANUAL-FIX-1087",
                source="manual_import",
            ),
        ],
        [PositionRef(sym, Decimal("14" if side == "buy" else "10"))],
    )
    block = blocks[sym]
    assert block["ledger_state"] == "known", block["unknown_reasons"]
    if side == "buy":
        assert block["same_day_buy_evidence"]["blocking_reasons"] == [
            "same_day_buy_fill_in_ledger"
        ]
        assert block["open_buy_evidence"]["blocking_reasons"] == [
            "same_day_buy_fill_order_not_proven_complete"
        ]
    else:
        assert block["open_sell_evidence"]["blocking_reasons"] == [
            "same_day_sell_fill_order_not_proven_complete"
        ]
        assert block["same_day_sell_evidence"]["blocking_reasons"] == [
            "same_day_sell_fill_in_ledger"
        ]


async def test_order_ledger_read_failure_degrades_both_order_views(db_session) -> None:
    class OrderReadFails:
        def __init__(self, inner):
            self._inner = inner

        async def execute(self, statement, *args, **kwargs):
            if "kis_live_order_ledger" in str(statement):
                raise RuntimeError("boom")
            return await self._inner.execute(statement, *args, **kwargs)

        async def rollback(self):
            await self._inner.rollback()

    run = _run(timedelta(minutes=10))
    run_ids = [run.run_id]  # the loader's rollback expires ORM attributes
    db_session.add_all([run, _fill()])
    await db_session.commit()
    try:
        blocks = await load_kis_live_kr_lot_blocks(
            OrderReadFails(db_session),  # type: ignore[arg-type]
            [PositionRef(SYM, Decimal("12"))],
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
    assert block["same_day_buy_evidence"]["state"] == "known"


async def test_get_holdings_opt_in_end_to_end_against_the_test_db(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_holdings(include_ledger_lots=True) with the real loader and session."""
    from tests.mcp_server import get_holdings_golden_support as support

    held, failing = "T108707", "T108708"
    support.install_fake_collect(
        monkeypatch,
        [
            support.kis_kr_position(held, 12.0, 1000.0),
            support.kis_kr_position(failing, 3.0, 1000.0),
        ],
    )
    run = _run(timedelta(minutes=10), now=datetime.now(UTC))
    run_ids = [run.run_id]
    db_session.add_all(
        [
            run,
            _fill(symbol=held, filled_at=datetime.now(UTC) - timedelta(days=40)),
            _order(
                f"{ORDER_PREFIX}41",
                symbol=held,
                trade_date=datetime.now(UTC),
                quantity=Decimal("4"),
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

    by_symbol = {
        p["symbol"]: p["ledger_lots"]
        for g in result["accounts"]
        for p in g["positions"]
        if "ledger_lots" in p
    }
    held_block = by_symbol[held]
    assert held_block["ledger_state"] == "known", held_block["unknown_reasons"]
    assert held_block["open_sell_evidence"]["blocking_reasons"] == [
        "own_nonterminal_sell_order_today"
    ]
    assert held_block["sellable_by_ledger"] == "8"
    failing_block = by_symbol[failing]
    assert failing_block["ledger_state"] == "unknown"
    assert failing_block["sellable_by_ledger"] is None
    assert failing_block["open_sell_evidence"]["state"] == "known"


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
    leaked = (
        await db_session.execute(
            select(func.count())
            .select_from(ExecutionLedger)
            .where(ExecutionLedger.symbol.in_(ALL_SYMBOLS))
        )
    ).scalar_one()
    orders = (
        await db_session.execute(
            select(func.count())
            .select_from(KISLiveOrderLedger)
            .where(KISLiveOrderLedger.order_no.like(f"{ORDER_PREFIX}%"))
        )
    ).scalar_one()
    assert (leaked, orders) == (0, 0)
