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
