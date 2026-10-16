"""ROB-1379 — one execution, one counted row, whichever writer lands first.

Every writer runs through its real code path against the run-owned test DB:

* fillwire → the HTTP ingest route (``POST .../fills/ingest``) with the exact
  Record JSON fillwire posts (raw ``{"tr", "fields", "received_at"}`` frame,
  ``fill_seq = sha256(fields joined by "^")``);
* the in-app monitor → ``UnifiedWebSocketMonitor._record_execution_ledger_fill``
  through the default direct-DB sink, fed by the real KIS parser output / the
  Upbit myOrder event;
* the reconciler → ``ExecutionLedgerReconciler.run(dry_run=False)`` over the
  real normalizers applied to KIS ``inquire-daily-ccld`` / overseas daily-order
  rows and the Upbit order detail.

The three writers derive three different ``fill_seq`` values for one execution,
so the unique key alone lets each land as its own row. Each scenario asserts
the physical row count and the two quantity readers, and the mutant tests
patch out the supersede / terminal-cover step to prove the assertions are
load-bearing (the mutant reproduces the duplicate).

Dates sit in 2099-11 and every symbol/order number is random, so rows never
meet production-shaped data; everything this file writes is deleted after.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import settings
from app.models.execution_ledger import ExecutionLedger, ExecutionLedgerReconcileRun
from app.routers import execution_ledger_ingest as ingest_module
from app.routers.execution_ledger_ingest import router as ingest_router
from app.services.execution_ledger import repository as repository_module
from app.services.execution_ledger.normalizers import (
    _normalize_kis_domestic_filled,
    _normalize_kis_overseas_filled,
    normalize_upbit_order,
)
from app.services.execution_ledger.quarantine import fillwire_fill_seq
from app.services.execution_ledger.reconciler import ExecutionLedgerReconciler
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.fill_notification import normalize_kis_fill, normalize_upbit_fill
from app.services.kis_websocket_internal.parsers import ExecutionMessageParser
from tests.services.execution_ledger._quarantine_fixtures import frame_fields

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_INGEST_PATH = "/trading/api/execution-ledger/fills/ingest"
TRADE_DAY = "20991112"
CUTOVER = datetime(2099, 11, 11, 15, 0, tzinfo=UTC)  # 2099-11-12 00:00 KST
WINDOW_START = datetime(2099, 11, 11, 15, 0, tzinfo=UTC)
WINDOW_END = datetime(2099, 11, 13, 15, 0, tzinfo=UTC)


def _order_no() -> str:
    # KIS domestic ODNO shape: 10 digits with leading zeros.
    return f"00{random.randint(10_000_000, 99_999_999)}"


def _kr_symbol() -> str:
    return f"9{random.randint(10_000, 99_999)}"


class _Ledger:
    """Tracks what one test wrote so teardown deletes exactly that."""

    def __init__(self, db) -> None:
        self.db = db
        self.order_ids: set[str] = set()
        self.run_ids: list[uuid.UUID] = []

    async def rows_for(self, order_id: str) -> list[ExecutionLedger]:
        await self.db.rollback()
        result = await self.db.execute(
            select(ExecutionLedger)
            .where(ExecutionLedger.broker_order_id == order_id)
            .order_by(ExecutionLedger.id.asc())
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def net(self, key: tuple[str, ...]) -> Decimal | None:
        await self.db.rollback()
        net = await ExecutionLedgerRepository(self.db).net_quantity_by_match_key_since(
            cutover=CUTOVER
        )
        return net.get(key)

    async def cleanup(self) -> None:
        await self.db.rollback()
        if self.order_ids:
            await self.db.execute(
                delete(ExecutionLedger).where(
                    ExecutionLedger.broker_order_id.in_(sorted(self.order_ids))
                )
            )
        if self.run_ids:
            await self.db.execute(
                delete(ExecutionLedgerReconcileRun).where(
                    ExecutionLedgerReconcileRun.run_id.in_(self.run_ids)
                )
            )
        await self.db.commit()


@pytest_asyncio.fixture
async def ledger(db_session, monkeypatch) -> AsyncIterator[_Ledger]:
    monkeypatch.setattr(settings, "EXECUTION_LEDGER_COMMIT_ENABLED", True)
    monkeypatch.setattr(settings, "kis_ws_is_mock", False)
    helper = _Ledger(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


@pytest_asyncio.fixture
async def ingest(db_session, monkeypatch) -> AsyncIterator[AsyncClient]:
    """The real ingest route; downstream (alerts) is out of this file's scope."""

    async def _no_downstream(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ingest_module, "run_post_upsert_downstream", _no_downstream)
    app = FastAPI()
    app.include_router(ingest_router)

    async def _override_db():
        yield db_session

    from app.core.db import get_db

    app.dependency_overrides[get_db] = _override_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        yield client


# ------------------------------------------------------------------ writers


def fillwire_record(
    *,
    order_no: str,
    symbol: str,
    qty: str,
    price: str,
    hhmmss: str,
    side_code: str = "02",
) -> dict[str, Any]:
    """The exact JSON fillwire's decode.Record marshals for a KIS execution."""
    fields = frame_fields(
        order_no=order_no,
        symbol=symbol,
        side=side_code,
        qty=qty,
        price=price,
        hhmmss=hhmmss,
        cntg_yn="2",
    )
    return {
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": symbol,
        "raw_symbol": symbol,
        "side": "buy" if side_code == "02" else "sell",
        "broker_order_id": order_no,
        "fill_seq": fillwire_fill_seq(fields),
        "filled_qty": qty,
        "filled_price": price,
        "filled_notional": None,
        "fee_amount": None,
        "fee_currency": "KRW",
        "filled_at": f"2099-11-12T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}+09:00",
        "currency": "KRW",
        "correlation_id": None,
        "raw_payload_json": {
            "tr": "H0STCNI0",
            "fields": fields,
            "received_at": f"2099-11-12T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}.2+09:00",
        },
    }


async def post_fillwire(
    client: AsyncClient, *records: dict[str, Any], run_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """POST one batch; a re-send of the same payload reuses its ``run_id``."""
    response = await client.post(
        _INGEST_PATH,
        json={
            "source": "fillwire",
            "source_run_id": str(run_id or uuid.uuid4()),
            "fills": list(records),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["results"]


async def monitor_kis_kr(
    *, order_no: str, symbol: str, qty: str, price: str, hhmmss: str
) -> str | None:
    """The in-app monitor path: KIS parser output -> direct-DB sink."""
    from websocket_monitor import UnifiedWebSocketMonitor

    fields = frame_fields(
        order_no=order_no, symbol=symbol, qty=qty, price=price, hhmmss=hhmmss
    )
    fields[13] = "2"  # CNTG_YN: a real execution
    event = ExecutionMessageParser({})._parse_domestic_execution(fields)
    assert event is not None
    # The parser stamps today's date on the HHMMSS clock; pin the trade day.
    event["filled_at"] = f"2099-11-12T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}+09:00"
    event["market"] = "kr"
    order = normalize_kis_fill(event)
    return await UnifiedWebSocketMonitor()._record_execution_ledger_fill(
        event, order, broker="kis", correlation_id=order_no
    )


async def reconcile(ledger: _Ledger, broker: str, rows: list[dict[str, Any]]) -> Any:
    async def _fetcher(**_kwargs: Any) -> dict[str, Any]:
        return {"orders": rows, "errors": []}

    reconciler = ExecutionLedgerReconciler(
        ExecutionLedgerRepository(ledger.db), fetcher=_fetcher
    )
    diff = await reconciler.run(
        broker,  # type: ignore[arg-type]
        start_at=WINDOW_START,
        end_at=WINDOW_END,
        dry_run=False,
    )
    ledger.run_ids.append(diff.source_run_id)
    await ledger.db.commit()
    return diff


def kis_kr_daily_row(
    *,
    order_no: str,
    symbol: str,
    ord_qty: str,
    tot_ccld_qty: str,
    price: str,
    rmn_qty: str,
    ord_tmd: str = "093000",
) -> dict[str, Any]:
    """One KIS ``inquire-daily-ccld`` (TTTC8001R) output1 row."""
    total = Decimal(tot_ccld_qty) * Decimal(price)
    return {
        "ord_dt": TRADE_DAY,
        "ord_gno_brno": "06010",
        "odno": order_no,
        "orgn_odno": "",
        "ord_dvsn_name": "지정가",
        "sll_buy_dvsn_cd": "02",
        "sll_buy_dvsn_cd_name": "현금매수",
        "pdno": symbol,
        "prdt_name": "테스트종목",
        "ord_qty": ord_qty,
        "ord_unpr": price,
        "ord_tmd": ord_tmd,
        "tot_ccld_qty": tot_ccld_qty,
        "avg_prvs": price,
        "cncl_yn": "",
        "tot_ccld_amt": str(total),
        "loan_dt": "",
        "ord_dvsn_cd": "00",
        "cncl_cfrm_qty": "0",
        "rmn_qty": rmn_qty,
        "rjct_qty": "0",
        "ccld_cndt_name": "없음",
        "excg_id_dvsn_cd": "SOR",
    }


def kr_key(symbol: str) -> tuple[str, ...]:
    return ("kis", "live", "krx", "equity_kr", symbol, "KRW")


# ------------------------------------------------------------------ KIS KR


async def test_kr_websocket_then_reconciler_counts_once(ledger, ingest) -> None:
    order_no, symbol = _order_no(), _kr_symbol()
    ledger.order_ids.add(order_no)

    [first] = await post_fillwire(
        ingest,
        fillwire_record(
            order_no=order_no, symbol=symbol, qty="10", price="5000", hhmmss="093001"
        ),
    )
    assert first["status"] == "inserted"
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_domestic_filled(
                kis_kr_daily_row(
                    order_no=order_no,
                    symbol=symbol,
                    ord_qty="10",
                    tot_ccld_qty="10",
                    price="5000",
                    rmn_qty="0",
                )
            )
        ],
    )

    rows = await ledger.rows_for(order_no)
    assert [r.source for r in rows] == ["websocket", "reconciler"]
    # The websocket row is superseded on read: the execution counts once.
    assert await ledger.net(kr_key(symbol)) == Decimal("10")


async def test_kr_reconciler_terminal_then_websocket_is_not_inserted(
    ledger, ingest
) -> None:
    order_no, symbol = _order_no(), _kr_symbol()
    ledger.order_ids.add(order_no)
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_domestic_filled(
                kis_kr_daily_row(
                    order_no=order_no,
                    symbol=symbol,
                    ord_qty="10",
                    tot_ccld_qty="10",
                    price="5000",
                    rmn_qty="0",
                )
            )
        ],
    )
    [reconciled] = await ledger.rows_for(order_no)

    # Two partial executions arrive late from fillwire (backlog flush).
    first = fillwire_record(
        order_no=order_no, symbol=symbol, qty="4", price="5000", hhmmss="093001"
    )
    second = fillwire_record(
        order_no=order_no, symbol=symbol, qty="6", price="5000", hhmmss="093002"
    )
    run_id = uuid.uuid4()
    results = await post_fillwire(ingest, first, second, run_id=run_id)
    assert results == [
        {"status": "unchanged", "row_id": reconciled.id, "reason": None},
        {"status": "unchanged", "row_id": reconciled.id, "reason": None},
    ]
    # The in-app monitor's own fill_seq for the same execution: same answer.
    status = await monitor_kis_kr(
        order_no=order_no, symbol=symbol, qty="4", price="5000", hhmmss="093001"
    )
    assert status == "unchanged"

    rows = await ledger.rows_for(order_no)
    assert [r.id for r in rows] == [reconciled.id]
    assert await ledger.net(kr_key(symbol)) == Decimal("10")

    # A replay (whole or partial batch) stays unchanged with the same row id.
    assert await post_fillwire(ingest, first, run_id=run_id) == [
        {"status": "unchanged", "row_id": reconciled.id, "reason": None}
    ]


async def test_kr_live_order_new_partial_is_kept_and_converges(ledger, ingest) -> None:
    """A still-live order is never a cover: a later partial fill is not lost."""
    order_no, symbol = _order_no(), _kr_symbol()
    ledger.order_ids.add(order_no)
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_domestic_filled(
                kis_kr_daily_row(
                    order_no=order_no,
                    symbol=symbol,
                    ord_qty="10",
                    tot_ccld_qty="3",
                    price="5000",
                    rmn_qty="7",
                )
            )
        ],
    )
    [late] = await post_fillwire(
        ingest,
        fillwire_record(
            order_no=order_no, symbol=symbol, qty="7", price="5000", hhmmss="100500"
        ),
    )
    assert late["status"] == "inserted"
    rows = await ledger.rows_for(order_no)
    assert sorted(r.source for r in rows) == ["reconciler", "websocket"]
    # Provisional until the broker aggregate catches up (existing read rule).
    assert await ledger.net(kr_key(symbol)) == Decimal("3")

    # The next reconcile updates the same aggregate row; nothing double counts.
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_domestic_filled(
                kis_kr_daily_row(
                    order_no=order_no,
                    symbol=symbol,
                    ord_qty="10",
                    tot_ccld_qty="10",
                    price="5000",
                    rmn_qty="0",
                )
            )
        ],
    )
    rows = await ledger.rows_for(order_no)
    assert sorted(r.source for r in rows) == ["reconciler", "websocket"]
    assert await ledger.net(kr_key(symbol)) == Decimal("10")


async def test_kr_replay_of_an_existing_websocket_key_keeps_its_row(
    ledger, ingest
) -> None:
    order_no, symbol = _order_no(), _kr_symbol()
    ledger.order_ids.add(order_no)
    first = fillwire_record(
        order_no=order_no, symbol=symbol, qty="4", price="5000", hhmmss="093001"
    )
    run_id = uuid.uuid4()
    [inserted] = await post_fillwire(ingest, first, run_id=run_id)
    assert inserted["status"] == "inserted"
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_domestic_filled(
                kis_kr_daily_row(
                    order_no=order_no,
                    symbol=symbol,
                    ord_qty="10",
                    tot_ccld_qty="10",
                    price="5000",
                    rmn_qty="0",
                )
            )
        ],
    )
    second = fillwire_record(
        order_no=order_no, symbol=symbol, qty="6", price="5000", hhmmss="093002"
    )
    [reconciled] = [
        r for r in await ledger.rows_for(order_no) if r.source != "websocket"
    ]
    # Partial re-send: the known key keeps its own row id, the new one is
    # covered by the terminal reconciler row.
    assert await post_fillwire(ingest, first, second, run_id=run_id) == [
        {"status": "unchanged", "row_id": inserted["row_id"], "reason": None},
        {"status": "unchanged", "row_id": reconciled.id, "reason": None},
    ]
    assert len(await ledger.rows_for(order_no)) == 2
    assert await ledger.net(kr_key(symbol)) == Decimal("10")


async def test_kr_same_order_number_on_another_day_is_not_covered(
    ledger, ingest
) -> None:
    """KIS reuses order numbers across days; the identity carries the day."""
    order_no, symbol = _order_no(), _kr_symbol()
    ledger.order_ids.add(order_no)
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_domestic_filled(
                kis_kr_daily_row(
                    order_no=order_no,
                    symbol=symbol,
                    ord_qty="10",
                    tot_ccld_qty="10",
                    price="5000",
                    rmn_qty="0",
                )
            )
        ],
    )
    record = fillwire_record(
        order_no=order_no, symbol=symbol, qty="10", price="5000", hhmmss="093001"
    )
    record["filled_at"] = "2099-11-13T09:30:01+09:00"
    [next_day] = await post_fillwire(ingest, record)
    assert next_day["status"] == "inserted"
    # And it is not superseded on read either: both executions count.
    assert await ledger.net(kr_key(symbol)) == Decimal("20")


async def test_kr_websocket_quantity_above_terminal_total_is_inserted(
    ledger, ingest
) -> None:
    order_no, symbol = _order_no(), _kr_symbol()
    ledger.order_ids.add(order_no)
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_domestic_filled(
                kis_kr_daily_row(
                    order_no=order_no,
                    symbol=symbol,
                    ord_qty="10",
                    tot_ccld_qty="4",
                    price="5000",
                    rmn_qty="0",
                )
            )
        ],
    )
    [result] = await post_fillwire(
        ingest,
        fillwire_record(
            order_no=order_no, symbol=symbol, qty="10", price="5000", hhmmss="093001"
        ),
    )
    # 10 cannot be inside a terminal total of 4: kept, so it surfaces.
    assert result["status"] == "inserted"


async def test_position_before_fill_counts_a_superseded_execution_once(
    ledger, ingest
) -> None:
    order_no, symbol = _order_no(), _kr_symbol()
    ledger.order_ids.add(order_no)
    await post_fillwire(
        ingest,
        fillwire_record(
            order_no=order_no, symbol=symbol, qty="10", price="5000", hhmmss="093001"
        ),
    )
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_domestic_filled(
                kis_kr_daily_row(
                    order_no=order_no,
                    symbol=symbol,
                    ord_qty="10",
                    tot_ccld_qty="10",
                    price="5000",
                    rmn_qty="0",
                )
            )
        ],
    )
    await ledger.db.rollback()
    qty_before, rows_before = await ExecutionLedgerRepository(
        ledger.db
    ).position_before_fill(
        broker="kis",
        account_mode="live",
        venue="krx",
        instrument_type="equity_kr",
        symbol=symbol,
        currency="KRW",
        filled_at=datetime(2099, 11, 12, 6, 0, tzinfo=UTC),
        ledger_id=2**62,
    )
    assert (qty_before, rows_before) == (Decimal("10"), 2)


# ------------------------------------------------------------------ KIS US


def kis_us_daily_row(*, order_no: str, symbol: str, qty: str, price: str) -> dict:
    """One KIS overseas daily-order (TTTS3035R) row, fully filled."""
    return {
        "ord_dt": TRADE_DAY,
        "ord_gno_brno": "06010",
        "odno": order_no,
        "orgn_odno": "",
        "sll_buy_dvsn_cd": "02",
        "sll_buy_dvsn_cd_name": "매수",
        "rvse_cncl_dvsn": "00",
        "pdno": symbol,
        "prdt_name": "TEST CORP",
        "ft_ord_qty": qty,
        "ft_ord_unpr3": price,
        "ft_ccld_qty": qty,
        "ft_ccld_unpr3": price,
        "ft_ccld_amt3": str(Decimal(qty) * Decimal(price)),
        "nccs_qty": "0",
        "prcs_stat_name": "완료",
        "ord_tmd": "233000",
        "tr_mket_name": "나스닥",
        "ovrs_excg_cd": "NASD",
        "tr_crcy_cd": "USD",
    }


async def test_us_reconciler_terminal_then_monitor_without_exchange_code(
    ledger,
) -> None:
    """The US websocket event has no exchange code (venue ``krx``), still one row."""
    from websocket_monitor import UnifiedWebSocketMonitor

    order_no = f"0{random.randint(100_000_000, 999_999_999)}"
    ledger.order_ids.add(order_no)
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_overseas_filled(
                kis_us_daily_row(
                    order_no=order_no, symbol="BRK/B", qty="2", price="480.50"
                )
            )
        ],
    )
    [reconciled] = await ledger.rows_for(order_no)
    assert reconciled.venue == "NASD"

    event = {
        "symbol": "BRK.B",
        "side": "bid",
        "order_id": order_no,
        "filled_price": 480.5,
        "filled_qty": 2,
        "filled_amount": 961.0,
        "filled_at": "2099-11-12T23:30:05+09:00",
        "market": "us",
        "currency": "USD",
        "execution_status": "filled",
    }
    status = await UnifiedWebSocketMonitor()._record_execution_ledger_fill(
        event, normalize_kis_fill(event), broker="kis", correlation_id=order_no
    )
    assert status == "unchanged"
    assert [r.id for r in await ledger.rows_for(order_no)] == [reconciled.id]


# ------------------------------------------------------------------ Upbit


def upbit_order_detail(*, uuid_: str, trade_uuid: str) -> dict[str, Any]:
    """Upbit ``GET /v1/order`` detail of a filled limit buy (one trade)."""
    return {
        "uuid": uuid_,
        "side": "bid",
        "ord_type": "limit",
        "price": "92800000",
        "state": "done",
        "market": "KRW-BTC",
        "created_at": "2099-11-12T09:30:00+09:00",
        "volume": "0.0003",
        "remaining_volume": "0",
        "reserved_fee": "13.92",
        "remaining_fee": "0",
        "paid_fee": "13.92",
        "locked": "0",
        "executed_volume": "0.0003",
        "trades_count": 1,
        "trades": [
            {
                "market": "KRW-BTC",
                "uuid": trade_uuid,
                "price": "92800000",
                "volume": "0.0003",
                "funds": "27840",
                "side": "bid",
                "created_at": "2099-11-12T09:30:01+09:00",
            }
        ],
    }


def upbit_my_order_trade(*, uuid_: str, trade_uuid: str) -> dict[str, Any]:
    """Upbit myOrder websocket event for the same trade (``state=trade``)."""
    return {
        "type": "myOrder",
        "code": "KRW-BTC",
        "uuid": uuid_,
        "ask_bid": "BID",
        "order_type": "limit",
        "state": "trade",
        "trade_uuid": trade_uuid,
        "price": 92800000,
        "avg_price": 92800000,
        "volume": 0.0003,
        "remaining_volume": 0,
        "executed_volume": 0.0003,
        "trades_count": 1,
        "paid_fee": 13.92,
        "executed_funds": 27840,
        "trade_fee": 13.92,
        "is_maker": True,
        "trade_timestamp": 4098126601000,  # 2099-11-12 09:30:01 KST
        "order_timestamp": 4098126600000,
        "timestamp": 4098126601010,
        "stream_type": "REALTIME",
    }


async def _monitor_upbit(event: dict[str, Any]) -> str | None:
    from websocket_monitor import UnifiedWebSocketMonitor

    return await UnifiedWebSocketMonitor()._record_execution_ledger_fill(
        event, normalize_upbit_fill(event), broker="upbit", correlation_id=event["uuid"]
    )


async def test_upbit_both_orders_count_once(ledger) -> None:
    btc_key = ("upbit", "live", "upbit_krw", "crypto", "BTC", "KRW")
    for websocket_first in (True, False):
        before = await ledger.net(btc_key) or Decimal("0")
        order_uuid, trade_uuid = str(uuid.uuid4()), str(uuid.uuid4())
        ledger.order_ids.add(order_uuid)
        event = upbit_my_order_trade(uuid_=order_uuid, trade_uuid=trade_uuid)
        detail = normalize_upbit_order(
            upbit_order_detail(uuid_=order_uuid, trade_uuid=trade_uuid)
        )
        if websocket_first:
            assert await _monitor_upbit(event) == "inserted"
            await reconcile(ledger, "upbit", detail)
            sources = [r.source for r in await ledger.rows_for(order_uuid)]
            assert sources == ["websocket", "reconciler"]
        else:
            await reconcile(ledger, "upbit", detail)
            assert await _monitor_upbit(event) == "unchanged"
            sources = [r.source for r in await ledger.rows_for(order_uuid)]
            assert sources == ["reconciler"]
        after = await ledger.net(btc_key) or Decimal("0")
        assert after - before == Decimal("0.0003")


# ------------------------------------------------------------------ mutants


async def test_mutant_without_read_supersede_double_counts(
    ledger, ingest, monkeypatch
) -> None:
    order_no, symbol = _order_no(), _kr_symbol()
    ledger.order_ids.add(order_no)
    await post_fillwire(
        ingest,
        fillwire_record(
            order_no=order_no, symbol=symbol, qty="10", price="5000", hhmmss="093001"
        ),
    )
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_domestic_filled(
                kis_kr_daily_row(
                    order_no=order_no,
                    symbol=symbol,
                    ord_qty="10",
                    tot_ccld_qty="10",
                    price="5000",
                    rmn_qty="0",
                )
            )
        ],
    )
    assert await ledger.net(kr_key(symbol)) == Decimal("10")
    monkeypatch.setattr(
        repository_module,
        "supersede_provisional",
        lambda rows, authority=None: (list(rows), 0),
    )
    assert await ledger.net(kr_key(symbol)) == Decimal("20")


async def test_mutant_without_terminal_cover_inserts_the_duplicate(
    ledger, ingest, monkeypatch
) -> None:
    order_no, symbol = _order_no(), _kr_symbol()
    ledger.order_ids.add(order_no)
    await reconcile(
        ledger,
        "kis",
        [
            _normalize_kis_domestic_filled(
                kis_kr_daily_row(
                    order_no=order_no,
                    symbol=symbol,
                    ord_qty="10",
                    tot_ccld_qty="10",
                    price="5000",
                    rmn_qty="0",
                )
            )
        ],
    )
    monkeypatch.setattr(repository_module, "terminal_cover", lambda fill, rows: None)
    [result] = await post_fillwire(
        ingest,
        fillwire_record(
            order_no=order_no, symbol=symbol, qty="10", price="5000", hhmmss="093001"
        ),
    )
    assert result["status"] == "inserted"
    assert len(await ledger.rows_for(order_no)) == 2
