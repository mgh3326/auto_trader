"""ROB-559 — per-symbol live order history (list_live_orders_for_symbol + endpoint).

NOTE: the test DB is shared/not-truncated between tests, so by-symbol queries must
use UNIQUE per-test symbols (a real symbol like KRW-BTC would match rows other
tests committed). Each test mints a random token symbol so only its own rows match.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 14, tzinfo=UTC)


def _tok() -> str:
    return uuid.uuid4().hex[:8].upper()


def _live(symbol: str, market: str, *, status="accepted", created_at=None, **kw):
    from app.models.review import LiveOrderLedger

    return LiveOrderLedger(
        trade_date=NOW,
        broker="upbit" if market == "crypto" else "kis",
        account_scope="upbit_live" if market == "crypto" else "kis_live",
        market=market,
        symbol=symbol,
        side=kw.get("side", "buy"),
        order_kind="limit",
        order_no=kw.get("order_no", f"live-{uuid.uuid4().hex[:10]}"),
        status=status,
        lifecycle_state=status,
        filled_qty=kw.get("filled_qty"),
        avg_fill_price=kw.get("avg_fill_price"),
        report_item_uuid=None,
        created_at=created_at if created_at is not None else NOW,
    )


def _kis(symbol: str, *, status="accepted", created_at=None, **kw):
    from app.models.review import KISLiveOrderLedger

    return KISLiveOrderLedger(
        trade_date=NOW,
        symbol=symbol,
        instrument_type="equity_kr",
        side=kw.get("side", "buy"),
        order_type="limit",
        order_no=kw.get("order_no", f"kis-{uuid.uuid4().hex[:10]}"),
        account_mode="kis_live",
        broker="kis",
        status=status,
        lifecycle_state=status,
        created_at=created_at if created_at is not None else NOW,
    )


def _toss(symbol: str, market: str, *, status="accepted", created_at=None, **kw):
    from app.models.review import TossLiveOrderLedger

    return TossLiveOrderLedger(
        trade_date=NOW,
        broker="toss",
        account_mode="toss_live",
        operation_kind="place",
        market=market,
        symbol=symbol,
        side=kw.get("side", "buy"),
        order_type="limit",
        client_order_id=kw.get("client_order_id", f"toss-{uuid.uuid4().hex[:10]}"),
        broker_order_id=kw.get("broker_order_id"),
        status=status,
        created_at=created_at if created_at is not None else NOW,
    )


async def test_crypto_matches_full_pair_only(session) -> None:
    from app.services.investment_reports.linked_orders import (
        list_live_orders_for_symbol,
    )

    t, u = _tok(), _tok()
    pair = f"KRW-{t}"
    btc = f"btc-{uuid.uuid4().hex[:8]}"
    session.add(
        _live(
            pair,
            "crypto",
            order_no=btc,
            status="filled",
            filled_qty=Decimal("0.01"),
            avg_fill_price=Decimal("96180000"),
        )
    )
    session.add(_live(f"KRW-{u}", "crypto"))  # other crypto symbol, must not match
    session.add(_live(t, "us"))  # base form on US, must not match crypto
    await session.flush()

    views = await list_live_orders_for_symbol(session, "crypto", pair)
    assert [v.order_no for v in views] == [btc]
    assert views[0].symbol == pair  # full pair preserved (not prefix-stripped)
    assert views[0].market == "crypto"
    assert views[0].status == "filled"
    assert views[0].filled_qty == Decimal("0.01")


async def test_crypto_lowercase_pair_is_upper_normalized(session) -> None:
    from app.services.investment_reports.linked_orders import (
        list_live_orders_for_symbol,
    )

    t = _tok()
    no = f"btc-{uuid.uuid4().hex[:8]}"
    session.add(_live(f"KRW-{t}", "crypto", order_no=no))
    await session.flush()
    views = await list_live_orders_for_symbol(session, "crypto", f"krw-{t}".lower())
    assert [v.order_no for v in views] == [no]


async def test_kr_merges_kis_and_toss(session) -> None:
    from app.services.investment_reports.linked_orders import (
        list_live_orders_for_symbol,
    )

    t, other = _tok(), _tok()
    k = f"kis-{uuid.uuid4().hex[:8]}"
    to = f"toss-{uuid.uuid4().hex[:8]}"
    session.add(_kis(t, order_no=k))
    session.add(_toss(t, "kr", broker_order_id=to))
    session.add(_toss(other, "kr"))  # other symbol
    await session.flush()

    views = await list_live_orders_for_symbol(session, "kr", t)
    order_nos = {v.order_no for v in views}
    assert order_nos == {k, to}
    kis_view = next(v for v in views if v.order_no == k)
    assert kis_view.market == "kr" and kis_view.account_scope == "kis_live"
    toss_view = next(v for v in views if v.order_no == to)
    assert toss_view.market == "kr" and toss_view.account_scope == "toss_live"


async def test_us_merges_live_and_toss(session) -> None:
    from app.services.investment_reports.linked_orders import (
        list_live_orders_for_symbol,
    )

    t = _tok()
    lv = f"live-{uuid.uuid4().hex[:8]}"
    to = f"toss-{uuid.uuid4().hex[:8]}"
    session.add(_live(t, "us", order_no=lv))
    session.add(_toss(t, "us", broker_order_id=to))
    await session.flush()

    views = await list_live_orders_for_symbol(session, "us", t)
    assert {v.order_no for v in views} == {lv, to}


async def test_days_cutoff_excludes_old(session) -> None:
    from app.services.investment_reports.linked_orders import (
        list_live_orders_for_symbol,
    )

    t = _tok()
    pair = f"KRW-{t}"
    recent = f"r-{uuid.uuid4().hex[:8]}"
    old = f"o-{uuid.uuid4().hex[:8]}"
    session.add(
        _live(
            pair,
            "crypto",
            order_no=recent,
            created_at=datetime.now(UTC) - timedelta(days=3),
        )
    )
    session.add(
        _live(
            pair,
            "crypto",
            order_no=old,
            created_at=datetime.now(UTC) - timedelta(days=400),
        )
    )
    await session.flush()

    views = await list_live_orders_for_symbol(session, "crypto", pair, days=90)
    assert [v.order_no for v in views] == [recent]


async def test_limit_caps_and_orders_recent_first(session) -> None:
    from app.services.investment_reports.linked_orders import (
        list_live_orders_for_symbol,
    )

    t = _tok()
    pair = f"KRW-{t}"
    base = datetime.now(UTC)
    nos = []
    for i in range(3):
        no = f"n{i}-{uuid.uuid4().hex[:6]}"
        nos.append(no)
        session.add(
            _live(pair, "crypto", order_no=no, created_at=base - timedelta(minutes=i))
        )  # i=0 newest
    await session.flush()

    views = await list_live_orders_for_symbol(session, "crypto", pair, limit=2)
    assert [v.order_no for v in views] == [nos[0], nos[1]]  # recent first, capped


async def test_empty_for_unknown_symbol_or_market(session) -> None:
    from app.services.investment_reports.linked_orders import (
        list_live_orders_for_symbol,
    )

    assert await list_live_orders_for_symbol(session, "crypto", f"KRW-{_tok()}") == []
    assert await list_live_orders_for_symbol(session, "weird", f"KRW-{_tok()}") == []


async def test_endpoint_returns_symbol_order_ledger(session) -> None:
    from app.routers.invest_api import get_stock_detail_order_ledger

    t = _tok()
    pair = f"KRW-{t}"
    no = f"btc-{uuid.uuid4().hex[:8]}"
    session.add(
        _live(
            pair,
            "crypto",
            order_no=no,
            status="filled",
            filled_qty=Decimal("0.02"),
            avg_fill_price=Decimal("90000000"),
        )
    )
    await session.flush()

    resp = await get_stock_detail_order_ledger(
        market="crypto", symbol=pair, user=object(), db=session, days=90, limit=50
    )
    assert resp.count == 1
    assert resp.items[0].order_no == no
    assert resp.items[0].symbol == pair
    assert resp.items[0].status == "filled"
    assert resp.items[0].filled_qty == Decimal("0.02")
