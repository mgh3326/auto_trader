"""Document-example fixture coverage for hk 6663 US mover sources."""

import asyncio
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services.brokers.kis.client import KISClient
from app.services.invest_momentum_events import us_intraday, us_sources
from app.services.research_candles.session_segment import US_PRE
from scripts import us_intraday_movers
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


def test_toss_reader_uses_supported_settings_without_credential_precheck(monkeypatch):
    sentinel = object()
    calls = []

    monkeypatch.setattr(
        us_intraday_movers.TossReadClient,
        "from_settings",
        lambda: calls.append("called") or sentinel,
    )
    monkeypatch.setattr(
        us_intraday_movers.settings, "toss_api_client_id", None, raising=False
    )
    monkeypatch.setattr(
        us_intraday_movers.settings, "toss_api_client_secret", None, raising=False
    )
    assert us_intraday_movers._build_toss_reader() is sentinel
    assert calls == ["called"]


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
        Decimal("0.625"),
    )
    assert apple.volume == 250000 and apple.trade_value == Decimal("50312500")
    assert berkshire.symbol == "BRK.B"
    assert apple.raw["row"]["price"]["changeRate"] == "0.00625"
    assert (
        apple.change_amount is None
        and apple.reasons["change_amount"] == "not_in_payload"
    )
    assert apple.name is None and apple.reasons["name"] == "not_in_payload"


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
    missing_name = us_sources.classify_benzinga_movers(
        {"result": {"gainers": [{"symbol": "AAPL"}], "losers": []}}, "up"
    )
    assert missing_name.rows[0].name is None
    assert missing_name.rows[0].reasons["name"] == "not_in_payload"


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
async def test_real_kis_client_facade_delegates_mover_ranking():
    class MarketData:
        async def inquire_overseas_mover_ranking(
            self, ranking_type, exchange_code, *, side, vol_rang="0"
        ):
            return {
                "ranking_type": ranking_type,
                "exchange_code": exchange_code,
                "side": side,
                "vol_rang": vol_rang,
            }

    client = object.__new__(KISClient)
    client._market_data = MarketData()
    assert await client.inquire_overseas_mover_ranking(
        "price_fluct", "NAS", side="up", vol_rang="4"
    ) == {
        "ranking_type": "price_fluct",
        "exchange_code": "NAS",
        "side": "up",
        "vol_rang": "4",
    }


@pytest.mark.asyncio
async def test_kis_ranking_received_time_is_taken_after_all_requests():
    requests = 0
    clock_request_counts = []

    class TimedKis(FakeKis):
        async def inquire_overseas_mover_ranking(
            self, source, exchange_code, *, side, vol_rang="0"
        ):
            nonlocal requests
            requests += 1
            return {"output1": {"stat": "R"}, "output2": []}

    def clock():
        clock_request_counts.append(requests)
        return PRE.astimezone(dt.UTC)

    await us_intraday._fetch_kis_ranking(
        TimedKis(),
        source=us_sources.SOURCE_KIS_PRICE_FLUCT,
        side="up",
        clock=clock,
    )
    assert requests == 3
    assert clock_request_counts == [3]


@pytest.mark.asyncio
async def test_collection_deadline_writes_timeout_status_for_unfinished_surfaces(
    monkeypatch,
):
    class SlowAlpaca(FakeAlpaca):
        async def screener_movers(self, *, top):
            await asyncio.sleep(1)

    monkeypatch.setattr(us_intraday, "MAX_COLLECTION_SECONDS", 0.001)
    monkeypatch.setattr(
        us_intraday, "InvestMomentumEventSnapshotsRepository", MemoryRepo
    )
    MemoryRepo.statuses.clear()
    MemoryRepo.rows.clear()
    report = await us_intraday.run_us_intraday_movers(
        alpaca=SlowAlpaca(),
        kis=FakeKis(),
        toss=FakeToss(),
        benzinga=FakeBenzinga(),
        session=FakeSession(),
        now=PRE,
        commit=True,
        clock=lambda: PRE.astimezone(dt.UTC),
        asset_types=_asset_types,
        segment_of=lambda _: US_PRE,
    )
    assert len(report.surfaces) == 15
    assert all(
        line.status == "error" and line.reason == "timeout" and line.written
        for line in report.surfaces
    )
    assert len(MemoryRepo.statuses) == 15


@pytest.mark.asyncio
async def test_one_new_source_failure_does_not_block_other_source_writes(monkeypatch):
    class BrokenOneKis(FakeKis):
        async def inquire_overseas_mover_ranking(
            self, source, exchange_code, *, side, vol_rang="0"
        ):
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
