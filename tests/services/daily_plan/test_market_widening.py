"""hk 6981: US/crypto plan rows and the explicit executable mode (6939 §1.1)."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.services.daily_plan import store
from app.services.daily_plan.bundle import SCHEMA_VERSION, canonical_bundle
from app.services.daily_plan.validator import normalize_symbol, prepare_row
from app.services.daily_plan.vocabulary import ACCOUNT_MODES
from tests.services.daily_plan.test_publish import fake_db, request

DAY = date(2026, 11, 24)


def _policy(*executable):
    return SimpleNamespace(
        version="v1",
        daily_plan=SimpleNamespace(
            no_add_floor_pct=-8,
            carry_max_sessions=5,
            max_adopt=10,
            executable_markets=frozenset(executable),
        ),
    )


POLICY = _policy()


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
        policy=_policy("crypto"),
        policy_hash="hash",
    )
    inserted = [
        call.args[0].compile().params
        for call in db.execute.call_args_list
        if getattr(call.args[0], "table", None) is store.rows
    ]
    assert [params["execution_state"] for params in inserted] == ["executable"]


@pytest.mark.asyncio
async def test_watch_carry_reads_the_previous_plan_of_the_same_market(monkeypatch):
    seen = []

    async def fake_get_plan(_db, **kwargs):
        seen.append(kwargs["market"])
        return None

    monkeypatch.setattr(store, "get_plan", fake_get_plan)
    now = datetime(2026, 11, 24, 3, tzinfo=UTC)
    row = _row(
        market="crypto",
        symbol="KRW-BTC",
        venue="UPBIT",
        session="crypto_24h",
        action="watch",
        side=None,
        trigger={"kind": "price_at_or_below", "value": "100"},
        carry_allowed=True,
    )
    await store.write_version(
        fake_db(current=0),
        day=DAY,
        request=request(market="crypto", asof=now.isoformat(), rows=[row]),
        now=now,
        policy=POLICY,
        policy_hash="hash",
    )
    assert seen == ["crypto"]


def _heads_db(existing_mode: str | None):
    """A DB holding at most one KR head; head lookups honour the mode filter."""
    head_id = uuid4()

    async def scalar(stmt):
        if stmt.get_final_froms()[0] is store.heads:
            params = stmt.compile().params
            wanted = [v for k, v in params.items() if k.startswith("mode")]
            if existing_mode is None or (wanted and wanted != [existing_mode]):
                return None
            return head_id
        return None

    return SimpleNamespace(
        scalar=AsyncMock(side_effect=scalar), execute=AsyncMock(return_value=None)
    )


def _inserted(db, table):
    return [
        call.args[0].compile().params
        for call in db.execute.call_args_list
        if getattr(call.args[0], "table", None) is table
    ]


def _bundle_body():
    closed = datetime(2026, 11, 24, 8, 20, tzinfo=ZoneInfo("Asia/Seoul"))
    body = {
        "schema_version": SCHEMA_VERSION,
        "bundle_closed_at": closed.isoformat(),
        "rows": [],
    }
    return body, hashlib.sha256(canonical_bundle(body)).hexdigest()


@pytest.mark.asyncio
async def test_bundle_first_creates_the_head_in_the_policy_mode():
    """r4714936282: with kr enabled, the 08:20 bundle makes an executable head."""
    body, digest = _bundle_body()
    db = _heads_db(None)
    await store.store_input_bundle(
        db, day=DAY, body=body, claimed_sha256=digest, policy=_policy("kr")
    )
    assert [head["mode"] for head in _inserted(db, store.heads)] == ["executable"]


@pytest.mark.asyncio
async def test_default_policy_bundle_head_is_plan_only():
    body, digest = _bundle_body()
    db = _heads_db(None)
    await store.store_input_bundle(
        db, day=DAY, body=body, claimed_sha256=digest, policy=POLICY
    )
    assert [head["mode"] for head in _inserted(db, store.heads)] == ["plan_only"]


@pytest.mark.asyncio
async def test_requested_executable_mode_is_refused_before_any_head():
    now = datetime(2026, 11, 24, 3, tzinfo=UTC)
    db = _heads_db(None)
    with pytest.raises(ValueError, match="plan_mode_conflict"):
        await store.write_version(
            db,
            day=DAY,
            request=request(mode="executable", asof=now.isoformat()),
            now=now,
            policy=POLICY,
            policy_hash="hash",
        )
    assert _inserted(db, store.heads) == []


@pytest.mark.asyncio
async def test_policy_flip_after_the_head_exists_is_refused():
    body, digest = _bundle_body()
    with pytest.raises(ValueError, match="plan_mode_conflict"):
        await store.store_input_bundle(
            _heads_db("plan_only"),
            day=DAY,
            body=body,
            claimed_sha256=digest,
            policy=_policy("kr"),
        )


def test_policy_file_enables_no_executable_market():
    from app.services.trading_policy_service import load_trading_policy

    assert load_trading_policy().daily_plan.executable_markets == frozenset()


@pytest.mark.parametrize(
    ("sizes", "refused"),
    [
        ({"would_be_notional": "1000000"}, False),
        ({"would_be_notional": "4999"}, True),
        ({"would_be_quantity": "0.0001"}, False),
        ({"would_be_quantity": "0.00000001"}, True),
    ],
)
def test_crypto_minimum_order_is_the_upbit_krw_minimum(sizes, refused):
    """r4714877846: a crypto notional need not buy one whole coin."""
    now = datetime(2026, 11, 24, 3, tzinfo=UTC)
    seen = (now - timedelta(minutes=5)).isoformat()
    row = {k: v for k, v in _row().items() if k != "would_be_quantity"}
    row.update(
        market="crypto",
        symbol="KRW-BTC",
        venue="UPBIT",
        session="crypto_24h",
        max_buy_price="140000000",
        would_be_limit_price="140000000",
        **sizes,
    )
    prepare = partial(
        prepare_row,
        row,
        evidence={"sources": {"price": {"event_at": seen, "first_observed_at": seen}}},
        asof=now,
        session_close=now + timedelta(hours=20),
        bundle_closed_at=now,
        oldest_accepted_at=now - timedelta(days=1),
        no_add_floor_pct=Decimal("-8"),
    )
    if refused:
        with pytest.raises(ValueError, match="below_min_order_amount"):
            prepare()
    else:
        assert prepare()["not_executable_reason"] is None


@pytest.mark.asyncio
async def test_confirm_links_reports_an_invalid_market(monkeypatch):
    """r4714876157: an unknown market is invalid_market, not version_not_found."""
    from app.mcp_server.tooling import daily_plan_registration as registration

    monkeypatch.setattr(registration.settings, "daily_plan_enabled", True)
    result = await registration.daily_plan_confirm_links(DAY, 1, market="eu")
    assert result == {"error_code": "invalid_market"}
