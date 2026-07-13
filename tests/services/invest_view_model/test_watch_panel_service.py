from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchAlert, InvestmentWatchEvent
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.services.invest_view_model.watch_panel_service import WatchPanelService
from tests._investment_reports_helpers import session  # noqa: F401


@pytest.mark.asyncio
async def test_watch_panel_service_list_watches(session: AsyncSession) -> None:  # noqa: F811
    # Clear only the snapshot symbols this test reasons about. We do NOT commit;
    # the rollback at teardown restores previous state.
    #
    # An unscoped `DELETE FROM market_quote_snapshots` cannot isolate us from
    # *concurrently committed* foreign rows anyway (under `pytest --dist=loadfile`
    # another file commits in a separate session, landing between our DELETE and
    # our read under READ COMMITTED) and it needlessly row-locks the whole table,
    # blocking peer suites. Instead the "must-have-no-snapshot" US alert below
    # uses a test-unique ticker (`WPUSNOSNAP`) that no other suite ever commits a
    # quote for — real tickers like AAPL collide with the Alpaca-paper suites.
    await session.execute(
        MarketQuoteSnapshot.__table__.delete().where(
            MarketQuoteSnapshot.symbol.in_(["005930", "WPUSNOSNAP", "BTC"])
        )
    )
    await session.flush()

    now = dt.datetime(2026, 6, 17, 12, 0, tzinfo=dt.UTC)

    # 1. Insert alert
    alert1 = InvestmentWatchAlert(
        alert_uuid=uuid.uuid4(),
        idempotency_key="key-1",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price_below",
        operator="below",
        threshold=Decimal("70000"),
        threshold_key="70000",
        intent="buy_review",
        action_mode="notify_only",
        rationale="test rationale",
        # Real watches carry a string[] checklist (ROB-599 prod 500 repro:
        # the schema previously typed this list[dict] → ValidationError → 500).
        trigger_checklist=["005930 현재가 조회 확인", "실주문 없음 확인"],
        valid_until=now + dt.timedelta(days=1),  # near_expiry will be True
        status="active",
    )
    session.add(alert1)

    alert2 = InvestmentWatchAlert(
        alert_uuid=uuid.uuid4(),
        idempotency_key="key-2",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="us",
        target_kind="asset",
        # Test-unique ticker: this alert must stay price-less to drive the
        # `degraded` data_state; a real ticker (AAPL) would race concurrently
        # committed snapshots from the Alpaca-paper suites under --dist=loadfile.
        symbol="WPUSNOSNAP",
        metric="price_above",
        operator="above",
        threshold=Decimal("200"),
        threshold_key="200",
        intent="sell_review",
        action_mode="notify_only",
        rationale="test rationale 2",
        valid_until=now + dt.timedelta(days=5),  # near_expiry will be False
        status="active",
    )
    session.add(alert2)

    # Triggered alert with event
    alert3 = InvestmentWatchAlert(
        alert_uuid=uuid.uuid4(),
        idempotency_key="key-3",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="crypto",
        target_kind="asset",
        symbol="BTC",
        metric="price_below",
        operator="below",
        threshold=Decimal("60000"),
        threshold_key="60000",
        intent="buy_review",
        action_mode="notify_only",
        rationale="test rationale 3",
        valid_until=now - dt.timedelta(days=1),
        status="triggered",
    )
    session.add(alert3)
    await session.flush()
    await session.refresh(alert3)

    event = InvestmentWatchEvent(
        event_uuid=uuid.uuid4(),
        idempotency_key="event-key-1",
        alert_id=alert3.id,
        source_report_uuid=alert3.source_report_uuid,
        source_item_uuid=alert3.source_item_uuid,
        market="crypto",
        target_kind="asset",
        symbol="BTC",
        metric="price_below",
        operator="below",
        threshold=Decimal("60000"),
        threshold_key="60000",
        intent="buy_review",
        action_mode="notify_only",
        current_value=Decimal("59000"),
        outcome="notified",
        correlation_id="corr-1",
        kst_date="2026-06-17",
        created_at=now,
    )
    session.add(event)

    # 2. Insert quote snapshots
    # Snapshot for 005930
    snapshot1 = MarketQuoteSnapshot(
        market="kr",
        symbol="005930",
        source="naver_finance",
        snapshot_at=now - dt.timedelta(minutes=5),
        price=Decimal("69800"),
    )
    session.add(snapshot1)
    await session.flush()

    service = WatchPanelService(db=session, clock=now)

    # Test list all
    resp = await service.list_watches(market="all", status="all")
    assert resp.count == 3
    assert resp.data_state == "degraded"  # WPUSNOSNAP has no snapshot price
    assert len(resp.warnings) == 1

    # Check alert1 fields (kr, 005930)
    row1 = next(item for item in resp.items if item.symbol == "005930")
    assert row1.near_expiry is True
    assert row1.current_price == Decimal("69800")
    assert row1.proximity_band == "hit"  # 69800 <= 70000 for price_below
    assert row1.last_event is None
    # String checklist round-trips (ROB-599 regression guard).
    assert row1.trigger_checklist == ["005930 현재가 조회 확인", "실주문 없음 확인"]

    # Check alert2 fields (us, WPUSNOSNAP — intentionally price-less)
    row2 = next(item for item in resp.items if item.symbol == "WPUSNOSNAP")
    assert row2.near_expiry is False
    assert row2.current_price is None
    assert row2.proximity_band is None

    # Check alert3 fields (crypto, BTC)
    row3 = next(item for item in resp.items if item.symbol == "BTC")
    assert row3.status == "triggered"
    assert row3.last_event is not None
    assert row3.last_event.outcome == "notified"
    assert row3.last_event.current_value == Decimal("59000")

    # Test filtering
    resp_kr = await service.list_watches(market="kr", status="active")
    assert resp_kr.count == 1
    assert resp_kr.items[0].symbol == "005930"

    # Per-symbol scope (ROB-592 stock detail page reuse)
    resp_symbol = await service.list_watches(market="kr", symbol="005930")
    assert resp_symbol.count == 1
    assert resp_symbol.items[0].symbol == "005930"
    # Proximity still enriched under the symbol scope (active price alert).
    assert resp_symbol.items[0].proximity_band == "hit"

    # Symbol that has no alert in this market returns empty (no crash).
    resp_symbol_none = await service.list_watches(market="us", symbol="005930")
    assert resp_symbol_none.count == 0


@pytest.mark.asyncio
async def test_watch_panel_service_us_symbol_normalization(
    session: AsyncSession,  # noqa: F811
) -> None:
    """US separator forms (BRK-B / BRK/B) resolve to the dot form the alert stores."""
    now = dt.datetime(2026, 6, 17, 12, 0, tzinfo=dt.UTC)

    alert = InvestmentWatchAlert(
        alert_uuid=uuid.uuid4(),
        idempotency_key="key-brk",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="us",
        target_kind="asset",
        symbol="BRK.B",  # alert table stores the dot form
        metric="price_above",
        operator="above",
        threshold=Decimal("500"),
        threshold_key="500",
        intent="sell_review",
        action_mode="notify_only",
        rationale="brk watch",
        valid_until=now + dt.timedelta(days=5),
        status="active",
    )
    session.add(alert)
    await session.flush()

    service = WatchPanelService(db=session, clock=now)

    # Hyphen and slash separators both canonicalize to BRK.B and match.
    for route_symbol in ("BRK-B", "brk-b", "BRK/B", "BRK.B"):
        resp = await service.list_watches(market="us", symbol=route_symbol)
        assert resp.count == 1, route_symbol
        assert resp.items[0].symbol == "BRK.B"


@pytest.mark.asyncio
async def test_watch_panel_service_skips_unbuildable_row(
    session: AsyncSession,  # noqa: F811
    monkeypatch,
) -> None:
    """One alert that fails to project must not 500 the whole panel (ROB-599).

    The good alert is still returned and the skipped count is surfaced in
    warnings (honest degrade for a read endpoint).
    """
    now = dt.datetime(2026, 6, 17, 12, 0, tzinfo=dt.UTC)

    def _alert(idem: str, symbol: str) -> InvestmentWatchAlert:
        return InvestmentWatchAlert(
            alert_uuid=uuid.uuid4(),
            idempotency_key=idem,
            source_report_uuid=uuid.uuid4(),
            source_item_uuid=uuid.uuid4(),
            market="us",
            target_kind="asset",
            symbol=symbol,
            metric="price_above",
            operator="above",
            threshold=Decimal("100"),
            threshold_key="100",
            intent="sell_review",
            action_mode="notify_only",
            rationale="r",
            valid_until=now + dt.timedelta(days=5),
            status="active",
        )

    session.add(_alert("good", "AAA"))
    session.add(_alert("bad", "BBB"))
    await session.flush()

    # Force the projection of the "BBB" alert to raise; the panel must survive.
    from app.services.invest_view_model import watch_panel_service as mod

    real_build = mod._build_watch_row

    def _flaky(alert, **kwargs):
        if alert.symbol == "BBB":
            raise ValueError("boom")
        return real_build(alert, **kwargs)

    monkeypatch.setattr(mod, "_build_watch_row", _flaky)

    service = WatchPanelService(db=session, clock=now)
    resp = await service.list_watches(market="us", status="all")

    assert resp.count == 1
    assert resp.items[0].symbol == "AAA"
    assert any("could not be displayed" in w for w in resp.warnings)
