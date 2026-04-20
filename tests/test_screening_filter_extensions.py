import pytest

from app.mcp_server.tooling.screening.common import (
    _apply_basic_filters,
    _kr_market_codes,
    _validate_screen_filters,
    normalize_screen_request,
)


def _request(**overrides):
    params = {
        "market": "kr",
        "asset_type": None,
        "category": None,
        "sector": None,
        "strategy": None,
        "sort_by": "volume",
        "sort_order": "desc",
        "min_market_cap": None,
        "max_per": None,
        "max_pbr": None,
        "min_dividend_yield": None,
        "min_dividend": None,
        "min_analyst_buy": None,
        "max_rsi": None,
        "limit": 10,
        "exclude_sectors": None,
        "instrument_types": None,
        "adv_krw_min": None,
        "market_cap_min_krw": None,
        "market_cap_max_krw": None,
    }
    params.update(overrides)
    return normalize_screen_request(**params)


def test_kr_market_codes_include_konex_and_all():
    assert _kr_market_codes("konex") == (["KNX"], "KNX")
    assert _kr_market_codes("all") == (["STK", "KSQ", "KNX"], "ALL")


def test_normalize_screen_request_new_filter_contract():
    request = _request(
        market="all",
        exclude_sectors=[" 반도체 ", "반도체", "Finance"],
        instrument_types=["common", "reit"],
        adv_krw_min=1_000_000_000,
        market_cap_min_krw=10_000_000_000,
        market_cap_max_krw=50_000_000_000,
    )

    assert request["market"] == "all"
    assert request["exclude_sectors"] == ["반도체", "Finance"]
    assert request["exclude_sector_keys"] == {"반도체", "finance"}
    assert request["instrument_types"] == ["common", "reit"]
    assert request["adv_krw_min"] == 1_000_000_000
    assert request["market_cap_min_krw"] == 10_000_000_000
    assert request["market_cap_max_krw"] == 50_000_000_000


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"instrument_types": ["common", "warrant"]}, "instrument_types"),
        ({"adv_krw_min": -1}, "adv_krw_min"),
        ({"market_cap_min_krw": -1}, "market_cap_min_krw"),
        ({"market_cap_max_krw": -1}, "market_cap_max_krw"),
        (
            {"sector": "Technology", "exclude_sectors": ["technology"]},
            "sector and exclude_sectors",
        ),
        (
            {"asset_type": "etf", "instrument_types": ["common"]},
            "asset_type='etf'",
        ),
        (
            {"market_cap_min_krw": 20, "market_cap_max_krw": 10},
            "market_cap_min_krw",
        ),
    ],
)
def test_normalize_screen_request_rejects_invalid_new_filters(overrides, match):
    with pytest.raises(ValueError, match=match):
        _request(**overrides)


def test_apply_basic_filters_supports_adv_instrument_sector_and_krw_market_cap():
    candidates = [
        {
            "symbol": "005930",
            "sector": "Technology",
            "instrument_type": "common",
            "adv_krw": 6_000_000_000,
            "market_cap_krw": 500_000_000_000,
        },
        {
            "symbol": "005935",
            "sector": "Technology",
            "instrument_type": "preferred",
            "adv_krw": 6_000_000_000,
            "market_cap_krw": 500_000_000_000,
        },
        {
            "symbol": "357120",
            "sector": "Real Estate",
            "instrument_type": "reit",
            "adv_krw": 7_000_000_000,
            "market_cap_krw": 300_000_000_000,
        },
        {
            "symbol": "000660",
            "sector": "Semiconductors",
            "instrument_type": "common",
            "adv_krw": 500_000_000,
            "market_cap_krw": 200_000_000_000,
        },
    ]

    filtered = _apply_basic_filters(
        candidates,
        min_market_cap=None,
        max_per=None,
        max_pbr=None,
        min_dividend_yield=None,
        max_rsi=None,
        adv_krw_min=1_000_000_000,
        market_cap_min_krw=250_000_000_000,
        market_cap_max_krw=600_000_000_000,
        instrument_types=["common"],
        exclude_sector_keys={"real estate"},
    )

    assert [item["symbol"] for item in filtered] == ["005930"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"adv_krw_min": 1},
        {"market_cap_min_krw": 1},
        {"market_cap_max_krw": 1},
        {"instrument_types": ["common"]},
        {"exclude_sectors": ["Technology"]},
    ],
)
def test_validate_screen_filters_rejects_new_filters_for_crypto(kwargs):
    with pytest.raises(ValueError, match="crypto"):
        _validate_screen_filters(
            market="crypto",
            asset_type=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            **kwargs,
        )
