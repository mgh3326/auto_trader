"""Gate-input timing contract (hk 5932, dots review B2).

Event time, source time and first-available time are stored apart; a value is usable
at a cutoff only if both its event time and its first-available time are <= cutoff.
"""

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.services.crypto_insight_snapshots.availability import (
    select_funding_window,
    timing_of,
    usable_at_cutoff,
)
from app.services.crypto_insight_snapshots.builder import _metric_to_payload
from app.services.crypto_insight_snapshots.repository import (
    CryptoInsightSnapshotsRepository,
)
from app.services.external import crypto_insights
from app.services.external.crypto_insights import (
    fetch_binance_settlement_funding_prints,
    fetch_upbit_binance_kimchi_premium,
)

HOUR = 3600
WED = dt.datetime(2026, 11, 18, 1, 0, tzinfo=dt.UTC)
SAT = dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.UTC)  # FX closed
SETTLE0 = dt.datetime(2026, 11, 17, 8, 0, tzinfo=dt.UTC)


def _ms(at: dt.datetime) -> int:
    return int(at.timestamp() * 1000)


def _clock(monkeypatch, at: dt.datetime) -> None:
    monkeypatch.setattr(crypto_insights, "_utc_now", lambda: at)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _funding_row(at: dt.datetime, rate="0.0001", **extra) -> dict:
    return {
        "symbol": "BTCUSDT",
        "fundingTime": _ms(at),
        "fundingRate": rate,
        "markPrice": "60000.0",
        **extra,
    }


def _prints(*gaps_hours: int, rates=("0.0001", "0.0002", "0.0003")) -> list[dict]:
    at = SETTLE0
    rows = [_funding_row(at, rates[0])]
    for gap, rate in zip(gaps_hours, rates[1:], strict=False):
        at = at + dt.timedelta(hours=gap)
        rows.append(_funding_row(at, rate))
    return rows


async def _funding(body, *, now=None):
    async with _client(lambda request: httpx.Response(200, json=body)) as client:
        return await fetch_binance_settlement_funding_prints(client, now=now)


def _row(metric) -> SimpleNamespace:
    """What the reader sees: a stored row with the redacted raw payload."""
    return SimpleNamespace(raw_payload=_metric_to_payload(metric).raw_payload)


# ---- fixture: arrives after cutoff ------------------------------------------------


@pytest.mark.asyncio
async def test_funding_collected_after_cutoff_is_not_usable_at_that_cutoff(monkeypatch):
    received = SETTLE0 + dt.timedelta(hours=30)
    _clock(monkeypatch, received)
    result = await _funding(_prints(8, 8))
    rows = [_row(m) for m in result.metrics]
    cutoff_before_receipt = received - dt.timedelta(hours=1)
    cutoff_at_receipt = received

    # Every settlement time is before the first cutoff, but we only had the values later.
    assert all(timing_of(r.raw_payload).event_at <= cutoff_before_receipt for r in rows)
    assert select_funding_window(rows, cutoff_before_receipt).unavailable_reason == (
        "missing"
    )
    window = select_funding_window(rows, cutoff_at_receipt).window
    assert window is not None and len(window.rows) == 3
    assert window.settlement_period_seconds == 8 * HOUR


@pytest.mark.asyncio
async def test_backfill_run_cannot_claim_an_earlier_first_available_time(monkeypatch):
    real_clock = SETTLE0 + dt.timedelta(days=3)
    _clock(monkeypatch, real_clock)
    # A backfill passes an old `now`; that may set freshness but not availability.
    result = await _funding(_prints(8, 8), now=SETTLE0 + dt.timedelta(hours=17))

    for metric in result.metrics:
        assert timing_of(_row(metric).raw_payload).first_available_at == real_clock


@pytest.mark.asyncio
async def test_kimchi_snapshot_at_is_event_time_not_the_injected_now(monkeypatch):
    _clock(monkeypatch, WED)
    upbit_ms = _ms(WED) - 3_000

    def handler(request):
        host = request.url.host
        if host == "api.upbit.com":
            return httpx.Response(
                200,
                json=[
                    {
                        "market": "KRW-BTC",
                        "trade_price": 80000000,
                        "trade_timestamp": upbit_ms,
                    }
                ],
            )
        if host == "api.binance.com":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "60000"})
        return httpx.Response(
            200,
            json={
                "time_last_update_unix": int(WED.timestamp()) - HOUR,
                "rates": {"KRW": 1300},
            },
        )

    past = WED - dt.timedelta(days=10)
    async with _client(handler) as client:
        result = await fetch_upbit_binance_kimchi_premium(client, now=past)

    (metric,) = result.metrics
    assert metric.observed_at == WED  # not `past`
    assert _metric_to_payload(metric).snapshot_at == WED
    row = _row(metric)
    assert usable_at_cutoff(row.raw_payload, WED)
    assert not usable_at_cutoff(row.raw_payload, past)


# ---- fixture: missing, then backfilled -------------------------------------------


@pytest.mark.asyncio
async def test_missing_then_backfilled_is_unknown_until_it_was_actually_received(
    monkeypatch,
):
    first_run = SETTLE0 + dt.timedelta(hours=17)  # only two prints published yet
    _clock(monkeypatch, first_run)
    partial = await _funding(_prints(8)[:2])
    assert partial.metrics == ()
    assert "expected 3 funding prints, got 2" in partial.warnings[0]

    second_run = SETTLE0 + dt.timedelta(hours=26)
    _clock(monkeypatch, second_run)
    rows = [_row(m) for m in (await _funding(_prints(8, 8))).metrics]

    # A cutoff between the two runs must not see the backfilled prints.
    between = SETTLE0 + dt.timedelta(hours=20)
    assert select_funding_window(rows, between).unavailable_reason == "missing"
    assert select_funding_window(rows, second_run).window is not None


# ---- fixture: settlement interval change -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("gaps", [(8, 4), (4, 8), (8, 3)])
async def test_funding_period_change_inside_the_window_writes_nothing(
    monkeypatch, gaps
):
    _clock(monkeypatch, SETTLE0 + dt.timedelta(days=2))
    result = await _funding(_prints(*gaps))

    assert result.metrics == ()
    assert "not consecutive on one settlement period" in result.warnings[0]


@pytest.mark.asyncio
async def test_funding_period_is_recorded_for_a_uniform_non_default_period(monkeypatch):
    _clock(monkeypatch, SETTLE0 + dt.timedelta(days=2))
    result = await _funding(_prints(4, 4))

    assert [m.raw_payload["settlement_period_seconds"] for m in result.metrics] == [
        4 * HOUR
    ] * 3


@pytest.mark.asyncio
async def test_reader_flags_a_period_change_across_stored_rows(monkeypatch):
    # Rows written by separate runs on either side of a 8h -> 4h switch.
    at = SETTLE0
    rows = []
    for gap in (None, 8, 4):
        at = at if gap is None else at + dt.timedelta(hours=gap)
        _clock(monkeypatch, at + dt.timedelta(minutes=1))
        rows.append(
            SimpleNamespace(
                raw_payload={
                    "timing": {
                        "event_at": at.isoformat(),
                        "source_at": None,
                        "first_available_at": (
                            at + dt.timedelta(minutes=1)
                        ).isoformat(),
                    }
                }
            )
        )
    result = select_funding_window(rows, at + dt.timedelta(hours=1))
    assert result.window is None and result.unavailable_reason == "period_changed"


# ---- fixture: stale source ---------------------------------------------------------


def _kimchi_handler(*, upbit_ms, fx_unix, binance="60000"):
    def handler(request):
        host = request.url.host
        if host == "api.upbit.com":
            return httpx.Response(
                200,
                json=[
                    {
                        "market": "KRW-BTC",
                        "trade_price": 80000000,
                        "trade_timestamp": upbit_ms,
                    }
                ],
            )
        if host == "api.binance.com":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": binance})
        return httpx.Response(
            200, json={"time_last_update_unix": fx_unix, "rates": {"KRW": 1300}}
        )

    return handler


async def _kimchi(handler):
    async with _client(handler) as client:
        return await fetch_upbit_binance_kimchi_premium(client)


@pytest.mark.asyncio
async def test_kimchi_stale_upbit_trade_writes_nothing(monkeypatch):
    _clock(monkeypatch, WED)
    result = await _kimchi(
        _kimchi_handler(
            upbit_ms=_ms(WED) - 10 * 60 * 1000, fx_unix=int(WED.timestamp()) - HOUR
        )
    )
    assert result.metrics == ()
    assert "stale upbit trade" in result.warnings[0]


@pytest.mark.asyncio
async def test_kimchi_stale_fx_on_a_weekday_writes_nothing(monkeypatch):
    _clock(monkeypatch, WED)
    result = await _kimchi(
        _kimchi_handler(
            upbit_ms=_ms(WED) - 1000, fx_unix=int(WED.timestamp()) - 40 * HOUR
        )
    )
    assert result.metrics == ()
    assert "stale usd/krw rate" in result.warnings[0]


@pytest.mark.asyncio
async def test_kimchi_weekend_fx_carry_is_allowed_and_recorded(monkeypatch):
    _clock(monkeypatch, SAT)
    friday_publish = dt.datetime(2026, 11, 20, 0, 5, tzinfo=dt.UTC)  # 49h old
    result = await _kimchi(
        _kimchi_handler(
            upbit_ms=_ms(SAT) - 1000, fx_unix=int(friday_publish.timestamp())
        )
    )
    (metric,) = result.metrics
    timing = metric.raw_payload["timing"]
    assert timing["fx_weekend_carry"] is True
    assert timing["fx_age_seconds"] == int((SAT - friday_publish).total_seconds())
    assert metric.raw_payload["inputs"]["usd_krw"]["market_closed_at_receipt"] is True


@pytest.mark.asyncio
async def test_kimchi_future_source_time_is_rejected(monkeypatch):
    _clock(monkeypatch, WED)
    result = await _kimchi(
        _kimchi_handler(upbit_ms=_ms(WED) + 60_000, fx_unix=int(WED.timestamp()) - HOUR)
    )
    assert result.metrics == ()
    assert "in the future" in result.warnings[0]


@pytest.mark.asyncio
async def test_kimchi_records_input_times_skew_and_the_usdt_assumption(monkeypatch):
    _clock(monkeypatch, WED)
    fx_unix = int(WED.timestamp()) - 2 * HOUR
    result = await _kimchi(_kimchi_handler(upbit_ms=_ms(WED) - 7_000, fx_unix=fx_unix))

    (metric,) = result.metrics
    raw = metric.raw_payload
    timing = raw["timing"]
    assert raw["inputs"]["upbit_krw"] == {
        "source_at": (WED - dt.timedelta(seconds=7)).isoformat(),
        "source_time_basis": "exchange_trade_timestamp",
    }
    assert raw["inputs"]["binance_usdt"]["source_at"] is None
    assert (
        raw["inputs"]["usd_krw"]["source_at"]
        == dt.datetime.fromtimestamp(fx_unix, tz=dt.UTC).isoformat()
    )
    assert timing["spot_skew_seconds"] == 7
    assert timing["fx_age_seconds"] == 2 * HOUR
    assert timing["max_skew_seconds"] == 2 * HOUR  # FX is the oldest input
    assert timing["fx_weekend_carry"] is False
    assert "USDT is treated as exactly 1 USD" in timing["usdt_usd_assumption"]
    assert timing["event_at"] == timing["first_available_at"] == WED.isoformat()


# ---- fixture: missing vs 0 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_funding_zero_is_a_real_print_and_missing_is_no_print(monkeypatch):
    _clock(monkeypatch, SETTLE0 + dt.timedelta(days=2))
    zero = await _funding(_prints(8, 8, rates=("0", "0.00000000", "-0.0")))
    assert [m.value for m in zero.metrics] == [Decimal(0)] * 3
    assert [m.raw_payload["value_state"] for m in zero.metrics] == ["zero"] * 3
    assert [m.label for m in zero.metrics] == ["neutral"] * 3

    for bad in (None, "", "n/a"):
        rows = _prints(8, 8)
        rows[1]["fundingRate"] = bad
        missing = await _funding(rows)
        assert missing.metrics == ()
        assert "missing or unreadable" in missing.warnings[0]
    rows = _prints(8, 8)
    del rows[2]["fundingRate"]
    assert (await _funding(rows)).metrics == ()


# ---- repository: first write wins ---------------------------------------------------


class _Existing(SimpleNamespace):
    pass


class _FakeSession:
    def __init__(self, existing=None):
        self.existing = existing
        self.added = []

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.existing)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_timing_rows_are_insert_only_and_never_rewritten(monkeypatch, caplog):
    _clock(monkeypatch, SETTLE0 + dt.timedelta(hours=17))
    (first, *_) = (await _funding(_prints(8, 8))).metrics
    payload = _metric_to_payload(first)

    session = _FakeSession()
    assert await CryptoInsightSnapshotsRepository(session).upsert([payload]) == 1
    stored = session.added[0]
    assert stored.raw_payload["timing"]["first_available_at"] == (
        (SETTLE0 + dt.timedelta(hours=17)).isoformat()
    )

    # A later run (different receipt time, revised rate) hits the same identity row.
    _clock(monkeypatch, SETTLE0 + dt.timedelta(days=5))
    (rerun, *_) = (await _funding(_prints(8, 8, rates=("0.5", "0.2", "0.3")))).metrics
    existing = _Existing(
        value=Decimal("0.0001"),
        raw_payload=dict(stored.raw_payload),
        updated_at=None,
    )
    session2 = _FakeSession(existing)
    with caplog.at_level("WARNING"):
        await CryptoInsightSnapshotsRepository(session2).upsert(
            [_metric_to_payload(rerun)]
        )

    assert session2.added == []
    assert existing.value == Decimal("0.0001")
    assert existing.raw_payload == stored.raw_payload
    assert existing.updated_at is None
    assert "later value ignored" in caplog.text


@pytest.mark.asyncio
async def test_rows_without_timing_keep_the_upsert_overwrite_behavior():
    from app.services.crypto_insight_snapshots.repository import (
        CryptoInsightSnapshotUpsert,
    )

    existing = _Existing(
        value=Decimal(1),
        unit=None,
        label=None,
        source_url=None,
        freshness_seconds=None,
        raw_payload=None,
        updated_at=None,
    )
    payload = CryptoInsightSnapshotUpsert(
        metric="funding_rate",
        provider="binance",
        symbol="BTCUSDT",
        value=Decimal(2),
        snapshot_at=WED,
    )
    await CryptoInsightSnapshotsRepository(_FakeSession(existing)).upsert([payload])
    assert existing.value == Decimal(2)


def test_rows_without_a_readable_timing_are_never_usable():
    for raw in (
        None,
        {},
        {"timing": {}},
        {"timing": {"event_at": "nope", "first_available_at": WED.isoformat()}},
        {"timing": {"event_at": WED.isoformat(), "first_available_at": "2026-11-18"}},
    ):
        assert not usable_at_cutoff(raw, WED + dt.timedelta(days=1))
    with pytest.raises(ValueError):
        usable_at_cutoff({}, dt.datetime(2026, 11, 18))
