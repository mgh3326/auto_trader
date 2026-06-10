from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.tvscreener_retry import TvScreenerError
from app.services.tvscreener_service import TvScreenerService


class _FakeStockQuery:
    def select(self, *columns: object) -> _FakeStockQuery:
        return self

    def set_range(self, start: int, end: int) -> _FakeStockQuery:
        return self

    def get(self) -> pd.DataFrame:
        return pd.DataFrame({"Symbol": ["NASDAQ:AAPL"]})


class _FakeStockScreener:
    def set_markets(self, *markets: object) -> None:
        pass

    def select(self, *columns: object) -> _FakeStockQuery:
        return _FakeStockQuery()


@pytest.mark.asyncio
async def test_tvscreener_stock_fetch_is_blocked_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener",
        lambda: SimpleNamespace(
            StockField=SimpleNamespace(),
            StockScreener=_FakeStockScreener,
        ),
    )

    service = TvScreenerService(max_retries=1, base_delay=0, timeout=0.1)

    with pytest.raises(TvScreenerError, match="disabled during pytest"):
        await service.query_stock_screener(columns=["symbol"], limit=1)
