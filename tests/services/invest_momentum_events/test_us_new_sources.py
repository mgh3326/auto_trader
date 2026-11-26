"""Document-example fixture coverage for hk 6663 US mover sources."""

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services.invest_momentum_events import us_intraday, us_sources
from app.services.research_candles.session_segment import US_PRE
from tests.services.invest_momentum_events.test_us_intraday import (
    FakeAlpaca,
    FakeBenzinga,
    FakeKis,
    FakeSession,
    FakeToss,
    MemoryRepo,
    _asset_types,
)

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/invest_momentum_events"
PRE = dt.datetime(2026, 11, 25, 4, 30, tzinfo=ZoneInfo("America/New_York"))


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    (
        "filename",
        "source",
        "side",
        "symbol",
        "price",
        "change_rate",
        "volume",
        "trade_value",
    ),
    [
        (
            "kis_overseas_price_fluct_doc_example.json",
            us_sources.SOURCE_KIS_PRICE_FLUCT,
            "up",
            "BRK.B",
            "512.34",
            "2.47",
            1200,
            "614808",
        ),
        (
            "kis_overseas_volume_surge_doc_example.json",
            us_sources.SOURCE_KIS_VOLUME_SURGE,
            "active",
            "AAPL",
            "201.25",
            "0.63",
            250000,
            "50312500",
        ),
        (
            "kis_overseas_new_highlow_doc_example.json",
            us_sources.SOURCE_KIS_NEW_HIGHLOW,
            "up",
            "NVDA",
            "140.50",
            "4.07",
            90000,
            "12645000.00",
        ),
        (
            "kis_overseas_volume_power_doc_example.json",
            us_sources.SOURCE_KIS_VOLUME_POWER,
            "active",
            "MSFT",
            "428.10",
            "0.49",
            80000,
            "34248000",
        ),
    ],
)
def test_kis_document_fixtures_normalize_exact_decimal_rows(
    filename, source, side, symbol, price, change_rate, volume, trade_value
):
    outcome = us_sources.classify_kis_mover_ranking(
        fixture(filename), "NAS", source=source, side=side
    )
    assert outcome.status == "ok"
    (row,) = outcome.rows
    assert (row.source, row.symbol, row.rank, row.market_type) == (
        source,
        symbol,
        1
        if source == us_sources.SOURCE_KIS_PRICE_FLUCT
        else int(fixture(filename)["output2"][0]["rank"]),
        "NAS",
    )
    assert row.price == Decimal(price)
    assert row.change_rate == Decimal(change_rate)
    assert row.volume == volume
    assert row.trade_value == Decimal(trade_value)
    assert (
        row.trade_value_basis == us_sources.BASIS_PROVIDER
        if "tamt" in fixture(filename)["output2"][0]
        else us_sources.BASIS_PRICE_TIMES_VOLUME
    )


def test_toss_document_fixture_preserves_ranked_amount_and_volume():
    toss_fixture = fixture("toss_rankings_doc_example.json")
    outcome = us_sources.classify_toss_rankings(
        toss_fixture, side="active", volume_payload=toss_fixture
    )
    assert outcome.status == "ok"
    apple, berkshire = outcome.rows
    assert (apple.symbol, apple.rank, apple.price, apple.change_rate) == (
        "AAPL",
        1,
        Decimal("201.25"),
        Decimal("0.00625"),
    )
    assert apple.volume == 250000 and apple.trade_value == Decimal("50312500")
    assert berkshire.symbol == "BRK.B"


def test_benzinga_document_fixture_maps_up_and_down_with_exact_amounts():
    up = us_sources.classify_benzinga_movers(
        fixture("benzinga_market_movers_doc_example.json"), "up"
    )
    down = us_sources.classify_benzinga_movers(
        fixture("benzinga_market_movers_doc_example.json"), "down"
    )
    assert up.status == down.status == "ok"
    assert up.rows[0].symbol == "NVDA" and up.rows[0].trade_value == Decimal("12645000")
    assert down.rows[0].symbol == "DELL" and down.rows[0].change_rate == Decimal(
        "-7.45"
    )


def test_new_source_missing_fields_stay_null_with_reasons():
    outcome = us_sources.classify_kis_mover_ranking(
        {"output2": [{"symb": "MSFT", "rank": "1"}]},
        "NAS",
        source=us_sources.SOURCE_KIS_NEW_HIGHLOW,
        side="up",
    )
    (row,) = outcome.rows
    assert row.price is row.change_rate is row.volume is row.trade_value is None
    assert {"price", "change_rate", "volume", "trade_value"} <= set(row.reasons)


@pytest.mark.asyncio
async def test_one_new_source_failure_does_not_block_other_source_writes(monkeypatch):
    class BrokenOneKis(FakeKis):
        async def inquire_overseas_mover_ranking(self, source, exchange_code, *, side):
            if source == us_sources.SOURCE_KIS_VOLUME_SURGE:
                raise RuntimeError("fixture failure")
            return {"output1": {"stat": "R"}, "output2": []}

    monkeypatch.setattr(
        us_intraday, "InvestMomentumEventSnapshotsRepository", MemoryRepo
    )
    MemoryRepo.statuses.clear()
    MemoryRepo.rows.clear()
    report = await us_intraday.run_us_intraday_movers(
        alpaca=FakeAlpaca(),
        kis=BrokenOneKis(),
        toss=FakeToss(),
        benzinga=FakeBenzinga(),
        session=FakeSession(),
        now=PRE,
        commit=True,
        clock=lambda: PRE.astimezone(dt.UTC),
        asset_types=_asset_types,
        segment_of=lambda _: US_PRE,
    )
    by_surface = {line.surface: line for line in report.surfaces}
    assert "us:kis_overseas_volume_surge:us_pre:active" in by_surface, list(by_surface)
    assert by_surface["us:kis_overseas_volume_surge:us_pre:active"].status == "error"
    assert by_surface["us:alpaca_screener:us_pre:up"].written
    assert any(
        row.source == us_sources.SOURCE_ALPACA for row in MemoryRepo.rows.values()
    )
    assert report.failed is True
