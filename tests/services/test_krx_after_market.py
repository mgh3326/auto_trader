"""#925 — KRX after-market eligibility: capability rules, store, list import."""

from __future__ import annotations

import datetime as dt

import pytest

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services import krx_after_market as kam
from app.services.kr_symbol_universe_service import (
    get_kr_krx_after_tradability,
    normalize_krx_after_list_symbols,
    replace_krx_after_market_list,
)
from app.services.krx_after_market import KrxAfterTradability
from scripts.import_krx_after_market_eligibility import parse_krx_after_list_text

_KST = dt.timezone(dt.timedelta(hours=9))
_NOW = dt.datetime(2026, 9, 29, 16, 5, tzinfo=_KST)


def _cap(**overrides) -> KrxAfterTradability:
    fields = {
        "listed": True,
        "exchange": "KOSPI",
        "security_type": "STOCK",
        "krx_trading_suspended": False,
        "asof": _NOW - dt.timedelta(hours=1),
        "list_source": "krx-notice-test",
    }
    fields.update(overrides)
    return KrxAfterTradability(**fields)


def test_listed_fresh_kospi_kosdaq_stock_is_tradable():
    assert _cap().krx_after_tradable is True
    assert _cap(exchange="KOSDAQ").krx_after_tradable is True
    assert _cap().reason == kam.REASON_TRADABLE


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"listed": None}, kam.REASON_LIST_MISSING),
        ({"listed": False}, kam.REASON_NOT_LISTED),
        ({"exchange": "KONEX"}, kam.REASON_EXCHANGE_INELIGIBLE),
        ({"exchange": None}, kam.REASON_EXCHANGE_INELIGIBLE),
        ({"security_type": None}, kam.REASON_SECURITY_TYPE_UNKNOWN),
        ({"security_type": "  "}, kam.REASON_SECURITY_TYPE_UNKNOWN),
        ({"security_type": "ETF"}, kam.REASON_ETP_EXCLUDED),
        ({"security_type": "etn"}, kam.REASON_ETP_EXCLUDED),
        ({"security_type": "REIT"}, kam.REASON_NON_STOCK_EXCLUDED),
        ({"krx_trading_suspended": True}, kam.REASON_KRX_SUSPENDED),
        ({"krx_trading_suspended": None}, kam.REASON_KRX_SUSPENDED_UNKNOWN),
    ],
)
def test_every_missing_or_negative_fact_is_not_tradable(overrides, reason):
    capability = _cap(**overrides)
    assert capability.reason == reason
    assert capability.krx_after_tradable is False
    assert capability.public_fields(now=_NOW)["krx_after_tradable"] is False


def test_listed_etf_is_false_even_on_the_list():
    fields = _cap(security_type="ETF").public_fields(now=_NOW)
    assert fields["krx_after_tradable"] is False
    assert fields["krx_after_tradable_reason"] == kam.REASON_ETP_EXCLUDED
    assert fields["krx_after_tradable_stale"] is False


def test_stale_boundary_is_exclusive_and_stale_reads_false_not_none():
    asof = _NOW - kam.KRX_AFTER_LIST_STALE_AFTER
    at_limit = _cap(asof=asof)
    past_limit = _cap(asof=asof - dt.timedelta(microseconds=1))
    assert at_limit.is_stale(now=_NOW) is False
    assert at_limit.public_fields(now=_NOW)["krx_after_tradable"] is True
    assert past_limit.is_stale(now=_NOW) is True
    fields = past_limit.public_fields(now=_NOW)
    assert fields["krx_after_tradable"] is False
    assert fields["krx_after_tradable_reason"] == kam.REASON_STALE_ASOF


def test_missing_asof_is_stale_and_false():
    fields = _cap(asof=None, listed=None).public_fields(now=_NOW)
    assert fields["krx_after_tradable"] is False
    assert fields["krx_after_tradable_stale"] is True
    assert fields["krx_after_tradable_reason"] == kam.REASON_MISSING_ASOF


def test_unknown_fields_are_false():
    fields = kam.krx_after_unknown_fields(kam.REASON_LOOKUP_FAILED)
    assert fields["krx_after_tradable"] is False
    assert fields["krx_after_tradable_reason"] == kam.REASON_LOOKUP_FAILED
    assert fields["krx_after_tradable_source"] == kam.KRX_AFTER_MARKET_SOURCE


def test_list_code_normalization_rejects_whole_list_on_bad_code():
    assert normalize_krx_after_list_symbols(["A005930", "375500", "5930"]) == [
        "005930",
        "375500",
    ]
    with pytest.raises(ValueError, match="invalid KRX after-market list code"):
        normalize_krx_after_list_symbols(["005930", "12345678"])
    with pytest.raises(ValueError):
        normalize_krx_after_list_symbols(["005930", "00-593"])


def test_list_file_parser_header_and_plain_forms():
    assert parse_krx_after_list_text(
        "종목코드,종목명\n005930,삼성전자\n375500,DL이앤씨\n"
    ) == [
        "005930",
        "375500",
    ]
    assert parse_krx_after_list_text("﻿단축코드\tname\nA000660\tx\n") == ["A000660"]
    assert parse_krx_after_list_text("005930\n375500\n") == ["005930", "375500"]
    with pytest.raises(ValueError, match="no code column"):
        parse_krx_after_list_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="row without a code"):
        parse_krx_after_list_text("종목코드,종목명\n,삼성전자\n")


def _universe(symbol: str, **overrides) -> KRSymbolUniverse:
    fields = {
        "symbol": symbol,
        "name": f"테스트{symbol}",
        "exchange": "KOSPI",
        "is_active": True,
        "nxt_eligible": False,
        "security_type": "STOCK",
        "krx_trading_suspended": False,
    }
    fields.update(overrides)
    return KRSymbolUniverse(**fields)


@pytest.mark.asyncio
async def test_store_reads_list_membership_and_universe_facts(db_session):
    db_session.add_all(
        [
            _universe("925001"),
            _universe("925002", security_type="ETF"),
            _universe("925003"),
            _universe("925004", is_active=False),
        ]
    )
    await db_session.flush()

    # No list imported yet: every symbol reads list-missing / not tradable.
    before = await get_kr_krx_after_tradability(["925001"], db=db_session)
    assert before["925001"].listed is None
    assert before["925001"].krx_after_tradable is False

    asof = dt.datetime.now(_KST)
    result = await replace_krx_after_market_list(
        db_session,
        symbols=["925001", "A925002", "925004", "925999"],
        list_asof=asof,
        list_source="krx-notice-test",
    )
    assert result.listed == 4
    assert result.unknown_symbols == ("925004", "925999")
    assert result.listed_non_stock == ("925002",)

    after = await get_kr_krx_after_tradability(
        ["925001", "925002", "925003", "925004", "925999"], db=db_session
    )
    assert after["925001"].krx_after_tradable is True
    assert after["925001"].list_source == "krx-notice-test"
    assert after["925002"].listed is True
    assert after["925002"].krx_after_tradable is False  # ETF on the list
    assert after["925003"].listed is False
    assert after["925003"].krx_after_tradable is False
    assert "925004" not in after  # inactive
    assert "925999" not in after  # not in universe

    # Full snapshot replacement: a second import drops 925001.
    second = await replace_krx_after_market_list(
        db_session,
        symbols=["925003"],
        list_asof=asof,
        list_source="krx-notice-test-2",
    )
    assert second.previous_rows == 4
    replaced = await get_kr_krx_after_tradability(["925001", "925003"], db=db_session)
    assert replaced["925001"].listed is False
    assert replaced["925003"].krx_after_tradable is True
    assert replaced["925003"].list_source == "krx-notice-test-2"
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"symbols": [], "list_source": "s"}, "empty"),
        ({"symbols": ["005930"], "list_source": "  "}, "list_source"),
        (
            {
                "symbols": ["005930"],
                "list_source": "s",
                "list_asof": dt.datetime(2026, 9, 29, 16),
            },
            "timezone-aware",
        ),
    ],
)
async def test_replace_rejects_unattested_lists(db_session, kwargs, match):
    params = {"list_asof": dt.datetime.now(_KST), **kwargs}
    with pytest.raises(ValueError, match=match):
        await replace_krx_after_market_list(db_session, **params)
    await db_session.rollback()
