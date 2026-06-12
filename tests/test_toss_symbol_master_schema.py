from sqlalchemy import inspect

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.us_symbol_universe import USSymbolUniverse


def test_kr_symbol_universe_has_toss_master_columns() -> None:
    columns = inspect(KRSymbolUniverse).columns
    for name in (
        "security_type",
        "is_common_share",
        "listing_status",
        "list_date",
        "delist_date",
        "shares_outstanding",
        "leverage_factor",
        "krx_trading_suspended",
        "nxt_trading_suspended",
        "isin",
        "toss_master_updated_at",
    ):
        assert name in columns


def test_us_symbol_universe_has_toss_master_columns() -> None:
    columns = inspect(USSymbolUniverse).columns
    for name in (
        "security_type",
        "is_common_share",
        "listing_status",
        "list_date",
        "delist_date",
        "shares_outstanding",
        "leverage_factor",
        "isin",
        "toss_master_updated_at",
    ):
        assert name in columns


def test_market_valuation_snapshot_allows_toss_openapi_source() -> None:
    constraints = MarketValuationSnapshot.__table_args__
    source_constraints = [
        c
        for c in constraints
        if "ck_market_valuation_snapshots_source" in getattr(c, "name", "")
    ]
    assert len(source_constraints) == 1
    assert "toss_openapi" in str(source_constraints[0].sqltext)
