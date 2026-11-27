"""hk 6981: US/crypto plan rows and the explicit executable mode (6939 §1.1)."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.services.daily_plan import store
from app.services.daily_plan.validator import normalize_symbol
from app.services.daily_plan.vocabulary import ACCOUNT_MODES
from tests.services.daily_plan.test_publish import fake_db, request

DAY = date(2026, 11, 24)
POLICY = SimpleNamespace(
    version="v1",
    daily_plan=SimpleNamespace(no_add_floor_pct=-8, carry_max_sessions=5, max_adopt=10),
)


@pytest.mark.parametrize(
    ("raw", "market", "venue", "segment", "expected"),
    [
        ("KRW-BTC", "crypto", "UPBIT", "crypto_24h", "KRW-BTC"),
        ("BTC", "crypto", "UPBIT", "crypto_24h", "KRW-BTC"),
        ("BRK/B", "us", "NYSE", "us_regular", "BRK.B"),
        ("BRK-B", "us", "NYSE", "us_pre", "BRK.B"),
        (" AAPL ", "us", "NASD", "us_post", "AAPL"),
        ("0256C0", "kr", "KRX", "regular", "0256C0"),
    ],
)
def test_per_market_normaliser(raw, market, venue, segment, expected):
    assert normalize_symbol(raw, market, venue, segment) == expected


@pytest.mark.parametrize(
    ("raw", "market", "venue", "segment"),
    [
        ("krw-btc", "crypto", "UPBIT", "crypto_24h"),
        ("USDT-BTC", "crypto", "UPBIT", "crypto_24h"),
        ("KRW.BTC", "crypto", "UPBIT", "crypto_24h"),
        ("brk.b", "us", "NYSE", "us_regular"),
        ("BRK.B.C", "us", "NYSE", "us_regular"),
        ("005930", "us", "NYSE", "us_regular"),
        ("0256C01", "kr", "KRX", "regular"),
        ("AAPL", "jp", "NASD", "us_regular"),
    ],
)
def test_wrong_shape_or_case_is_refused(raw, market, venue, segment):
    with pytest.raises(ValueError, match="invalid_symbol"):
        normalize_symbol(raw, market, venue, segment)


@pytest.mark.parametrize(
    ("market", "symbol", "venue", "segment"),
    [
        ("us", "AAPL", "KRX", "regular"),
        ("us", "AAPL", "NASD", "regular"),
        ("us", "AAPL", "LSE", "us_regular"),
        ("crypto", "KRW-BTC", "UPBIT", "us_regular"),
        ("kr", "005930", "UPBIT", "crypto_24h"),
    ],
)
def test_unknown_venue_or_session_is_refused(market, symbol, venue, segment):
    with pytest.raises(ValueError, match="invalid_venue_session"):
        normalize_symbol(symbol, market, venue, segment)


def test_account_modes_gain_upbit_live_and_mocks():
    assert {"upbit_live", "kis_mock", "kiwoom_mock", "alpaca_paper"} <= set(
        ACCOUNT_MODES
    )


def _row(**overrides):
    return {**request()["rows"][0], **overrides}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "now", "row"),
    [
        (
            "us",
            datetime(2026, 11, 24, 15, tzinfo=UTC),
            _row(market="us", symbol="BRK/B", venue="NYSE", session="us_regular"),
        ),
        (
            "us",
            datetime(2026, 11, 24, 10, tzinfo=UTC),
            _row(market="us", symbol="AAPL", venue="NASD", session="us_pre"),
        ),
        (
            "crypto",
            datetime(2026, 11, 24, 3, tzinfo=UTC),
            _row(market="crypto", symbol="BTC", venue="UPBIT", session="crypto_24h"),
        ),
    ],
)
async def test_us_and_crypto_rows_publish(market, now, row):
    result = await store.write_version(
        fake_db(current=0),
        day=DAY,
        request=request(market=market, asof=now.isoformat(), rows=[row]),
        now=now,
        policy=POLICY,
        policy_hash="hash",
    )
    assert result["status"] == "PUBLISHED"
    [published] = result["rows"]
    assert published["market"] == market
    assert (published["venue"], published["session"]) == (row["venue"], row["session"])
    assert published["symbol"] in ("BRK.B", "AAPL", "KRW-BTC")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "row", "code"),
    [
        (
            {"market": "us"},
            _row(market="us", symbol="AAPL", venue="NASD", session="regular"),
            "invalid_venue_session",
        ),
        ({"market": "us"}, _row(), "market_mismatch"),
        ({"market": "jp"}, _row(), "invalid_market"),
        ({"mode": "live"}, _row(), "invalid_plan_mode"),
    ],
)
async def test_unknown_market_mode_venue_or_session_is_refused(overrides, row, code):
    now = datetime(2026, 11, 24, 15, tzinfo=UTC)
    with pytest.raises(ValueError, match=code):
        await store.write_version(
            fake_db(current=0),
            day=DAY,
            request=request(asof=now.isoformat(), rows=[row], **overrides),
            now=now,
            policy=POLICY,
            policy_hash="hash",
        )


@pytest.mark.asyncio
async def test_executable_mode_is_stored_on_rows():
    now = datetime(2026, 11, 24, 3, tzinfo=UTC)
    db = fake_db(current=0)
    await store.write_version(
        db,
        day=DAY,
        request=request(
            market="crypto",
            mode="executable",
            asof=now.isoformat(),
            rows=[
                _row(
                    market="crypto",
                    symbol="KRW-ETH",
                    venue="UPBIT",
                    session="crypto_24h",
                )
            ],
        ),
        now=now,
        policy=POLICY,
        policy_hash="hash",
    )
    inserted = [
        call.args[0].compile().params
        for call in db.execute.call_args_list
        if getattr(call.args[0], "table", None) is store.rows
    ]
    assert [params["execution_state"] for params in inserted] == ["executable"]
