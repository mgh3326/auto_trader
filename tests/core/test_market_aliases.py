"""One market alias table for MCP tools and services."""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.market import (
    CRYPTO_MARKET_ALIASES,
    KR_MARKET_ALIASES,
    US_MARKET_ALIASES,
    market_instrument_type,
    market_key,
)
from app.core.timezone import KST, today_kst
from app.mcp_server.tooling.market_normalization import (
    normalize_equity_market,
    normalize_market_with_crypto,
)
from app.mcp_server.tooling.shared import normalize_market
from app.services.domain_errors import ValidationError
from app.services.market_data.service import _normalize_market as service_market

_ALL = (
    [(a, "kr") for a in KR_MARKET_ALIASES]
    + [(a, "us") for a in US_MARKET_ALIASES]
    + [(a, "crypto") for a in CRYPTO_MARKET_ALIASES]
)
_TYPE = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


def test_alias_sets_are_disjoint():
    assert not KR_MARKET_ALIASES & US_MARKET_ALIASES
    assert not KR_MARKET_ALIASES & CRYPTO_MARKET_ALIASES
    assert not US_MARKET_ALIASES & CRYPTO_MARKET_ALIASES


@pytest.mark.parametrize(("alias", "key"), _ALL)
def test_every_entry_point_resolves_the_same_alias_the_same_way(alias, key):
    spelled = f"  {alias.upper()} "
    assert market_key(spelled) == key
    assert market_instrument_type(spelled) == _TYPE[key]
    assert normalize_market(spelled) == _TYPE[key]
    assert service_market(spelled) == _TYPE[key]
    assert normalize_market_with_crypto(spelled) == key
    if key != "crypto":
        assert normalize_equity_market(spelled) == key


@pytest.mark.parametrize("value", [None, "", "   ", "jp", "binance"])
def test_unknown_values_keep_each_callers_policy(value):
    assert market_key(value) is None
    assert normalize_market(value) is None
    with pytest.raises(ValidationError):
        service_market(value)


def test_today_kst_is_the_kst_calendar_date():
    before = dt.datetime.now(KST).date()
    got = today_kst()
    after = dt.datetime.now(KST).date()
    assert before <= got <= after
