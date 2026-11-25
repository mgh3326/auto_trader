"""Persist an authenticated Benzinga Pro capture as one transaction."""

from datetime import datetime
from decimal import Decimal, localcontext

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.benzinga_pro_ingest import BenzingaProCapture, IngestCounts
from app.services.benzinga_http import parse_provider_time
from app.services.invest_momentum_events.models import MomentumEventUpsert
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.invest_momentum_events.us_intraday import (
    load_symbol_masters,
    run_id_for,
    surface_key,
    us_trading_date,
)
from app.services.invest_momentum_events.us_sources import (
    ASSET_UNKNOWN,
    derive_asset_type,
)
from app.services.market_events.repository import MarketEventsRepository

SOURCE = "benzinga_pro"
_MAX_TRADE_VALUE = Decimal("9999999999999999999999999999.99")


def _trade_value(price: Decimal | None, volume: int | None) -> Decimal | None:
    if price is None or volume is None:
        return None
    with localcontext() as context:
        context.prec = 60
        value = price * volume
    return value if 0 <= value <= _MAX_TRADE_VALUE else None


async def store_capture(db: AsyncSession, capture: BenzingaProCapture) -> IngestCounts:
    captured_at = datetime.fromisoformat(capture.captured_at)
    run_id = run_id_for(captured_at)
    snapshots = InvestMomentumEventSnapshotsRepository(db)
    counts = IngestCounts()
    if await snapshots.benzinga_capture_exists(run_id):
        return counts

    trading_date = us_trading_date(captured_at)
    symbols = sorted(
        {
            row.symbol
            for row in (*capture.movers.up, *capture.movers.down)
            if row.excluded_reason is None
        }
    )
    masters = await load_symbol_masters(db, symbols)
    for side, rows in (("up", capture.movers.up), ("down", capture.movers.down)):
        surface = surface_key(SOURCE, capture.session, side)
        # Symbol normalization can collapse two raw spellings of the same ticker.
        # Prefer the best rank before recording the unique row.
        unique = {}
        for row in sorted(rows, key=lambda item: item.rank):
            unique.setdefault(row.symbol, row)
        await snapshots.insert_run_status(
            run_id=run_id,
            snapshot_at=captured_at,
            trading_date=trading_date,
            surface=surface,
            status="ok" if unique else "empty",
            reason=None if unique else "no_rows",
            row_count=len(unique),
        )
        counts.runs_inserted += 1
        for row in unique.values():
            excluded = row.excluded_reason
            price = Decimal(row.price) if row.price is not None else None
            volume = int(Decimal(row.volume)) if row.volume is not None else None
            master = masters.get(row.symbol)
            was_inserted = await snapshots.insert_momentum(
                MomentumEventUpsert(
                    snapshot_at=captured_at,
                    first_received_at=captured_at,
                    trading_date=trading_date,
                    source=SOURCE,
                    surface=surface,
                    market="us",
                    trade_type="US",
                    market_type="US",
                    order_type="excluded" if excluded else side,
                    rank=row.rank,
                    symbol=row.symbol,
                    price=price,
                    volume=volume,
                    change_rate=(
                        Decimal(row.change_pct)
                        if row.change_pct is not None and excluded is None
                        else None
                    ),
                    market_cap=(
                        Decimal(row.market_cap) if row.market_cap is not None else None
                    ),
                    trade_value=(
                        _trade_value(price, volume) if excluded is None else None
                    ),
                    asset_type=derive_asset_type(master) if master else ASSET_UNKNOWN,
                    field_reasons={"change_rate": excluded} if excluded else None,
                    raw_payload=row.model_dump(),
                )
            )
            if was_inserted:
                if excluded:
                    counts.excluded_inserted += 1
                else:
                    counts.movers_inserted += 1

    events = MarketEventsRepository(db)
    # Stable ordering avoids conflicting lock acquisition for overlapping captures.
    for event in sorted(capture.breaking_events, key=lambda item: item.provider_id):
        try:
            created = parse_provider_time(event.created_raw)
        except OverflowError:
            created = None
        counts.events_inserted += await events.insert_provider_event(
            {
                "category": "breaking_news",
                "market": "us",
                "source": SOURCE,
                "status": "released",
                "source_event_id": event.provider_id,
                "symbol": event.symbols[0] if event.symbols else None,
                "title": event.headline,
                "event_date": us_trading_date(created or captured_at),
                "release_time_utc": created,
                "provider_created_at": created,
                "provider_created_raw": event.created_raw,
                "source_url": event.url,
                "first_received_at": captured_at,
                "fetched_at": captured_at,
                "raw_payload_json": {"symbols": event.symbols},
            }
        )
    return counts
