import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.order_proposals.buying_power import (
    BuyingPowerCache,
    BuyingPowerKey,
    build_create_advisory,
    currency_for_market,
    default_buying_power_reader,
    pending_buy_requirement,
    required_cash,
)
from app.services.order_proposals.service import OrderProposalsService, RungInput


@pytest.mark.unit
def test_required_cash_prefers_preview_value_and_fee():
    assert required_cash(
        quantity=Decimal("3"),
        limit_price=Decimal("71100"),
        preview={"estimated_value": "213300", "fee": "32"},
    ) == Decimal("213332")


@pytest.mark.unit
def test_required_cash_falls_back_to_limit_notional():
    assert required_cash(
        quantity=Decimal("3"),
        limit_price=Decimal("71100"),
        preview={"estimated_value": None, "fee": None},
    ) == Decimal("213300")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("market", "currency"),
    [("equity_kr", "KRW"), ("equity_us", "USD")],
)
def test_currency_for_supported_equity_market(market, currency):
    assert currency_for_market(market) == currency


@pytest.mark.asyncio
async def test_cache_single_flight_and_reservation_adjustment():
    cache = BuyingPowerCache(ttl_seconds=1.0)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return Decimal("100000")

    key = BuyingPowerKey("toss_live", None, "KRW")
    first, second = await asyncio.gather(
        cache.get_or_load(key, loader),
        cache.get_or_load(key, loader),
    )
    claim = await cache.claim(key, Decimal("30000"), loader)

    assert (first, second, calls) == (
        Decimal("100000"),
        Decimal("100000"),
        1,
    )
    assert claim.available == Decimal("100000")
    assert claim.token is not None
    assert await cache.get_or_load(key, loader) == Decimal("70000")
    assert calls == 1


@pytest.mark.asyncio
async def test_cache_does_not_store_loader_failure():
    cache = BuyingPowerCache(ttl_seconds=1.0)
    key = BuyingPowerKey("toss_live", None, "KRW")
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary outage")
        return Decimal("50000")

    with pytest.raises(RuntimeError, match="temporary outage"):
        await cache.get_or_load(key, loader)

    assert await cache.get_or_load(key, loader) == Decimal("50000")
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_slow_loader_still_single_flights_waiters():
    now = 0.0
    cache = BuyingPowerCache(ttl_seconds=1.0, clock=lambda: now)
    key = BuyingPowerKey("toss_live", None, "KRW")
    calls = 0

    async def loader():
        nonlocal calls, now
        calls += 1
        await asyncio.sleep(0)
        now = 2.0
        return Decimal("100000")

    first, second = await asyncio.gather(
        cache.get_or_load(key, loader),
        cache.get_or_load(key, loader),
    )

    assert (first, second) == (Decimal("100000"), Decimal("100000"))
    assert calls == 1


@pytest.mark.asyncio
async def test_cache_claim_allows_only_one_concurrent_underfunded_submit():
    cache = BuyingPowerCache(ttl_seconds=1.0)
    key = BuyingPowerKey("toss_live", None, "KRW")
    loader_calls = 0
    submit_calls = 0

    async def loader():
        nonlocal loader_calls
        loader_calls += 1
        await asyncio.sleep(0)
        return Decimal("100000")

    async def submit_if_claimed():
        nonlocal submit_calls
        claim = await cache.claim(key, Decimal("60000"), loader)
        if claim.token is not None:
            submit_calls += 1
            await asyncio.sleep(0)
        return claim

    claims = await asyncio.gather(submit_if_claimed(), submit_if_claimed())

    assert sorted(claim.available for claim in claims) == [
        Decimal("40000"),
        Decimal("100000"),
    ]
    assert sum(claim.token is not None for claim in claims) == 1
    assert loader_calls == 1
    assert submit_calls == 1


@pytest.mark.asyncio
async def test_expired_claim_release_cannot_erase_new_cache_generation_claim():
    now = 0.0
    cache = BuyingPowerCache(ttl_seconds=1.0, clock=lambda: now)
    key = BuyingPowerKey("toss_live", None, "KRW")

    async def loader():
        return Decimal("100000")

    old_claim = await cache.claim(key, Decimal("60000"), loader)
    assert old_claim.token is not None

    now = 2.0
    new_claim = await cache.claim(key, Decimal("60000"), loader)
    assert new_claim.token is not None
    await cache.release(key, old_claim.token)

    blocked = await cache.claim(key, Decimal("60000"), loader)
    assert blocked.available == Decimal("40000")
    assert blocked.token is None


@pytest.mark.asyncio
async def test_claim_ttl_starts_when_claim_is_created_not_snapshot_loaded():
    now = 0.0
    cache = BuyingPowerCache(ttl_seconds=1.0, clock=lambda: now)
    key = BuyingPowerKey("toss_live", None, "KRW")

    async def loader():
        return Decimal("100000")

    assert await cache.get_or_load(key, loader) == Decimal("100000")
    now = 0.9
    claim = await cache.claim(key, Decimal("60000"), loader)
    assert claim.token is not None

    now = 1.1
    blocked = await cache.claim(key, Decimal("60000"), loader)
    assert blocked.available == Decimal("40000")
    assert blocked.token is None


@pytest.mark.asyncio
async def test_default_toss_reader_skips_when_provider_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "toss_api_enabled", False)

    def forbidden_client():
        raise AssertionError("disabled Toss provider must not construct a client")

    monkeypatch.setattr(
        "app.services.brokers.toss.client.TossReadClient.from_settings",
        forbidden_client,
    )

    assert (
        await default_buying_power_reader(
            account_mode="toss_live",
            broker_account_id=None,
            currency="KRW",
        )
        is None
    )


async def _seed_proposal(
    db_session,
    *,
    side: str,
    quantity: str,
    limit_price: str | None,
    broker_account_id: str | None,
    valid_until: datetime | None = None,
    now: datetime | None = None,
):
    return await OrderProposalsService(db_session).create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        side=side,
        order_type="limit" if limit_price is not None else "market",
        proposer="test",
        broker_account_id=broker_account_id,
        valid_until=valid_until,
        now=now,
        rungs=[
            RungInput(
                0,
                side,
                Decimal(quantity),
                Decimal(limit_price) if limit_price is not None else None,
                None,
            )
        ],
    )


@pytest.mark.asyncio
async def test_pending_requirement_sums_only_same_account_pending_limit_buys(
    db_session,
):
    await _seed_proposal(
        db_session,
        side="buy",
        quantity="2",
        limit_price="100000",
        broker_account_id="account-a",
    )
    await _seed_proposal(
        db_session,
        side="buy",
        quantity="5",
        limit_price="100000",
        broker_account_id="account-a",
    )
    await _seed_proposal(
        db_session,
        side="buy",
        quantity="9",
        limit_price="100000",
        broker_account_id="account-b",
    )
    await _seed_proposal(
        db_session,
        side="sell",
        quantity="9",
        limit_price="100000",
        broker_account_id="account-a",
    )
    await _seed_proposal(
        db_session,
        side="buy",
        quantity="1",
        limit_price=None,
        broker_account_id="account-a",
    )

    required, skipped = await pending_buy_requirement(
        db_session,
        account_mode="toss_live",
        broker_account_id="account-a",
        currency="KRW",
        now=datetime.now(UTC),
    )

    assert required == Decimal("700000")
    assert skipped == 1


@pytest.mark.asyncio
async def test_create_advisory_reports_exact_pending_shortfall(db_session):
    await _seed_proposal(
        db_session,
        side="buy",
        quantity="7",
        limit_price="100000",
        broker_account_id="account-a",
    )

    async def reader(**kwargs):
        assert kwargs == {
            "account_mode": "toss_live",
            "broker_account_id": "account-a",
            "currency": "KRW",
        }
        return Decimal("500000")

    advisory = await build_create_advisory(
        db_session,
        account_mode="toss_live",
        broker_account_id="account-a",
        currency="KRW",
        now=datetime.now(UTC),
        buying_power_reader=reader,
    )

    assert advisory == {
        "status": "insufficient",
        "currency": "KRW",
        "buying_power": "500000",
        "pending_required": "700000",
        "shortfall": "200000",
        "skipped_market_rungs": 0,
        "warning": ("매수가능 500,000원 / 승인대기 필요 700,000원 → 부족 200,000원"),
    }


@pytest.mark.asyncio
async def test_create_advisory_is_unavailable_when_reader_fails(db_session):
    async def reader(**kwargs):
        raise RuntimeError("account api unavailable")

    advisory = await build_create_advisory(
        db_session,
        account_mode="toss_live",
        broker_account_id="account-unavailable",
        currency="KRW",
        now=datetime.now(UTC),
        buying_power_reader=reader,
    )

    assert advisory == {
        "status": "unavailable",
        "currency": "KRW",
        "buying_power": None,
        "pending_required": "0",
        "shortfall": None,
        "skipped_market_rungs": 0,
        "warning": None,
    }


# ROB-897 cause (2): pending_buy_requirement/build_create_advisory must ignore
# groups whose valid_until has already passed (no sweeper marks them expired
# yet), otherwise stale proposals inflate pending_required and produce a
# false "부족(shortfall)" warning for new, individually-affordable buy rungs.


@pytest.mark.asyncio
async def test_pending_requirement_excludes_past_valid_until_group(db_session):
    creation_now = datetime.now(UTC)
    stale_valid_until = creation_now + timedelta(seconds=1)
    await _seed_proposal(
        db_session,
        side="buy",
        quantity="3",
        limit_price="100000",
        broker_account_id="account-a",
        valid_until=stale_valid_until,
        now=creation_now,
    )

    query_now = creation_now + timedelta(minutes=5)  # past stale_valid_until

    required, skipped = await pending_buy_requirement(
        db_session,
        account_mode="toss_live",
        broker_account_id="account-a",
        currency="KRW",
        now=query_now,
    )

    assert required == Decimal("0")
    assert skipped == 0


@pytest.mark.asyncio
async def test_pending_requirement_sums_future_and_null_valid_until_groups(
    db_session,
):
    creation_now = datetime.now(UTC)

    # Still-live group with an explicit future expiry.
    future_valid_until = creation_now + timedelta(days=1)
    await _seed_proposal(
        db_session,
        side="buy",
        quantity="2",
        limit_price="100000",
        broker_account_id="account-a",
        valid_until=future_valid_until,
        now=creation_now,
    )

    # No-expiry group (valid_until IS NULL) never expires, so must still count.
    no_expiry_group = await _seed_proposal(
        db_session,
        side="buy",
        quantity="1",
        limit_price="100000",
        broker_account_id="account-a",
        valid_until=future_valid_until,
        now=creation_now,
    )
    no_expiry_group.valid_until = None
    await db_session.flush()

    query_now = creation_now + timedelta(minutes=5)

    required, skipped = await pending_buy_requirement(
        db_session,
        account_mode="toss_live",
        broker_account_id="account-a",
        currency="KRW",
        now=query_now,
    )

    assert required == Decimal("300000")
    assert skipped == 0


@pytest.mark.asyncio
async def test_create_advisory_no_false_shortfall_from_stale_expired_group(
    db_session,
):
    """Modeled on the ROB-897 numbers: a stale, past-expiry rung must not push
    an otherwise-affordable live rung into a reported shortfall."""
    creation_now = datetime.now(UTC)

    # Stale group: valid_until already passed by the time the advisory query
    # runs. Its 500,000 notional previously inflated pending_required past
    # available cash.
    stale_valid_until = creation_now + timedelta(seconds=1)
    await _seed_proposal(
        db_session,
        side="buy",
        quantity="5",
        limit_price="100000",
        broker_account_id="account-a",
        valid_until=stale_valid_until,
        now=creation_now,
    )

    # Live, individually-affordable group (259,700 notional).
    live_valid_until = creation_now + timedelta(days=1)
    await _seed_proposal(
        db_session,
        side="buy",
        quantity="2597",
        limit_price="100",
        broker_account_id="account-a",
        valid_until=live_valid_until,
        now=creation_now,
    )

    query_now = creation_now + timedelta(minutes=5)

    async def reader(**kwargs):
        return Decimal("285471")

    advisory = await build_create_advisory(
        db_session,
        account_mode="toss_live",
        broker_account_id="account-a",
        currency="KRW",
        now=query_now,
        buying_power_reader=reader,
    )

    assert advisory["status"] == "sufficient"
    assert advisory["pending_required"] == "259700"
    assert advisory["shortfall"] == "0"
    assert advisory["warning"] is None
