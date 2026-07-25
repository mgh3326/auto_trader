from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.invest_view_model.stock_detail_symbol_resolver import (
    SymbolNotFound,
    resolve_symbol,
)


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class FakeSession:
    def __init__(self, row):
        self.row = row
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarResult(self.row)


@pytest.mark.asyncio
async def test_resolve_symbol_kr_happy_path():
    db = FakeSession(
        SimpleNamespace(
            symbol="005930", name="삼성전자", exchange="KOSPI", is_active=True
        )
    )

    resolved = await resolve_symbol("kr", "005930", db)

    assert resolved.symbol_db == "005930"
    assert resolved.display_name == "삼성전자"
    assert resolved.currency == "KRW"
    assert resolved.asset_category == "kr_stock"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["BRK-B", "BRK/B", "BRK.B"])
async def test_resolve_symbol_us_normalizes_hyphen_and_slash_to_dot(raw):
    db = FakeSession(
        SimpleNamespace(
            symbol="BRK.B",
            name_kr="버크셔해서웨이 B",
            name_en="Berkshire Hathaway B",
            exchange="NYSE",
            is_active=True,
        )
    )

    resolved = await resolve_symbol("us", raw, db)

    assert resolved.symbol_db == "BRK.B"
    assert resolved.display_name == "버크셔해서웨이 B"
    assert resolved.exchange == "NYSE"
    assert resolved.currency == "USD"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_symbol", "expected_lookup"),
    [
        ("BTC", "KRW-BTC"),
        ("btc", "KRW-BTC"),
        ("BTC-KRW", "KRW-BTC"),
        ("KRW-BTC", "KRW-BTC"),
    ],
)
async def test_resolve_symbol_crypto_normalizes_route_inputs_to_upbit_market(
    raw_symbol: str, expected_lookup: str
):
    db = FakeSession(
        SimpleNamespace(
            market="KRW-BTC",
            base_currency="BTC",
            quote_currency="KRW",
            korean_name="비트코인",
            english_name="Bitcoin",
            is_active=True,
        )
    )

    resolved = await resolve_symbol("crypto", raw_symbol, db)

    compiled = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert f"upbit_symbol_universe.market = '{expected_lookup}'" in compiled
    assert resolved.symbol_db == "KRW-BTC"
    assert resolved.display_name == "비트코인"
    assert resolved.exchange == "KRW"
    assert resolved.asset_type == "crypto"


@pytest.mark.asyncio
async def test_resolve_symbol_unknown_raises_symbol_not_found():
    db = FakeSession(None)

    with pytest.raises(SymbolNotFound):
        await resolve_symbol("kr", "000000", db)
