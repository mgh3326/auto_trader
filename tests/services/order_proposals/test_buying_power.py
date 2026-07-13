import asyncio
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
    await cache.reserve(key, Decimal("30000"))

    assert (first, second, calls) == (
        Decimal("100000"),
        Decimal("100000"),
        1,
    )
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
):
    return await OrderProposalsService(db_session).create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        side=side,
        order_type="limit" if limit_price is not None else "market",
        proposer="test",
        broker_account_id=broker_account_id,
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
