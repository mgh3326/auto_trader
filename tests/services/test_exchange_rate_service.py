from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import exchange_rate_service as mod


@pytest.fixture(autouse=True)
def clear_exchange_rate_cache() -> None:
    mod._cache.clear()


def test_parse_toss_usd_krw_quote_uses_mid_rate_as_default() -> None:
    quote = mod._parse_toss_usd_krw_quote(
        {
            "baseCurrency": "USD",
            "quoteCurrency": "KRW",
            "rate": "1522.2",
            "midRate": "1522.05",
            "basisPoint": "15.2",
            "rateChangeType": "UP",
            "validFrom": "2026-06-12T09:30:00+09:00",
            "validUntil": "2026-06-12T09:31:00+09:00",
        }
    )

    assert quote.source == "toss"
    assert quote.rate == pytest.approx(1522.2)
    assert quote.mid_rate == pytest.approx(1522.05)
    assert quote.default_rate == pytest.approx(1522.05)
    assert quote.basis_point == pytest.approx(15.2)
    assert quote.rate_change_type == "UP"
    assert quote.valid_from == datetime(2026, 6, 12, 0, 30, tzinfo=UTC)
    assert quote.valid_until == datetime(2026, 6, 12, 0, 31, tzinfo=UTC)


def test_parse_open_er_api_quote_exposes_same_rate_and_mid_rate() -> None:
    quote = mod._parse_open_er_api_usd_krw_quote({"rates": {"KRW": 1498.7}})

    assert quote.source == "open_er_api"
    assert quote.rate == pytest.approx(1498.7)
    assert quote.mid_rate == pytest.approx(1498.7)
    assert quote.default_rate == pytest.approx(1498.7)
    assert quote.valid_from is None
    assert quote.valid_until is None


@pytest.mark.asyncio
async def test_get_usd_krw_rate_details_uses_toss_when_enabled(monkeypatch) -> None:
    toss_quote = mod.UsdKrwExchangeRateQuote(
        rate=1522.2,
        mid_rate=1522.05,
        source="toss",
        valid_until=datetime(2026, 6, 12, 0, 31, tzinfo=UTC),
    )
    fallback_called = False

    async def fake_toss() -> mod.UsdKrwExchangeRateQuote:
        return toss_quote

    async def fake_fallback() -> mod.UsdKrwExchangeRateQuote:
        nonlocal fallback_called
        fallback_called = True
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7,
            mid_rate=1498.7,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.settings, "toss_api_enabled", True)
    monkeypatch.setattr(mod, "_fetch_toss_usd_krw_quote", fake_toss)
    monkeypatch.setattr(mod, "_fetch_open_er_api_usd_krw_quote", fake_fallback)

    quote = await mod._fetch_usd_krw_rate_details()

    assert quote is toss_quote
    assert fallback_called is False


@pytest.mark.asyncio
async def test_get_usd_krw_rate_details_uses_fallback_when_toss_disabled(
    monkeypatch,
) -> None:
    async def fail_toss() -> mod.UsdKrwExchangeRateQuote:
        raise AssertionError("Toss should not be called when disabled")

    async def fake_fallback() -> mod.UsdKrwExchangeRateQuote:
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7,
            mid_rate=1498.7,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.settings, "toss_api_enabled", False)
    monkeypatch.setattr(mod, "_fetch_toss_usd_krw_quote", fail_toss)
    monkeypatch.setattr(mod, "_fetch_open_er_api_usd_krw_quote", fake_fallback)

    quote = await mod._fetch_usd_krw_rate_details()

    assert quote.source == "open_er_api"
    assert quote.default_rate == pytest.approx(1498.7)


@pytest.mark.asyncio
async def test_get_usd_krw_rate_details_falls_back_when_toss_fails(
    monkeypatch,
) -> None:
    async def fail_toss() -> mod.UsdKrwExchangeRateQuote:
        raise RuntimeError("Toss is unavailable")

    async def fake_fallback() -> mod.UsdKrwExchangeRateQuote:
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7,
            mid_rate=1498.7,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.settings, "toss_api_enabled", True)
    monkeypatch.setattr(mod, "_fetch_toss_usd_krw_quote", fail_toss)
    monkeypatch.setattr(mod, "_fetch_open_er_api_usd_krw_quote", fake_fallback)

    quote = await mod._fetch_usd_krw_rate_details()

    assert quote.source == "open_er_api"
    assert quote.default_rate == pytest.approx(1498.7)


@pytest.mark.asyncio
async def test_cache_uses_toss_valid_until(monkeypatch) -> None:
    calls = 0
    now_utc = datetime(2026, 6, 12, 0, 30, 0, tzinfo=UTC)
    monotonic_now = 1000.0

    async def fake_fetch() -> mod.UsdKrwExchangeRateQuote:
        nonlocal calls
        calls += 1
        return mod.UsdKrwExchangeRateQuote(
            rate=1522.2 + calls,
            mid_rate=1522.05 + calls,
            source="toss",
            valid_until=datetime(2026, 6, 12, 0, 31, 0, tzinfo=UTC),
        )

    monkeypatch.setattr(mod, "_now_utc", lambda: now_utc)
    monkeypatch.setattr(mod.time, "monotonic", lambda: monotonic_now)
    monkeypatch.setattr(mod, "_fetch_usd_krw_rate_details", fake_fetch)

    first = await mod.get_usd_krw_rate_details()
    second = await mod.get_usd_krw_rate_details()

    assert first is second
    assert calls == 1

    monotonic_now = 1059.9
    third = await mod.get_usd_krw_rate_details()

    assert third is first
    assert calls == 1

    monotonic_now = 1060.1
    fourth = await mod.get_usd_krw_rate_details()

    assert fourth is not first
    assert fourth.mid_rate == pytest.approx(1524.05)
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_uses_fixed_ttl_for_open_er_api(monkeypatch) -> None:
    calls = 0
    monotonic_now = 2000.0

    async def fake_fetch() -> mod.UsdKrwExchangeRateQuote:
        nonlocal calls
        calls += 1
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7 + calls,
            mid_rate=1498.7 + calls,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.time, "monotonic", lambda: monotonic_now)
    monkeypatch.setattr(mod, "_fetch_usd_krw_rate_details", fake_fetch)

    first = await mod.get_usd_krw_rate_details()
    monotonic_now = 2299.9
    second = await mod.get_usd_krw_rate_details()

    assert second is first
    assert calls == 1

    monotonic_now = 2300.1
    third = await mod.get_usd_krw_rate_details()

    assert third is not first
    assert calls == 2
