from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)


def _row(
    symbol: str, partition: str, t: datetime, close: float, source: str = "kis"
) -> DailyCandleRow:
    return DailyCandleRow(
        time_utc=t,
        symbol=symbol,
        partition=partition,
        open=close - 1.0,
        high=close + 0.5,
        low=close - 1.5,
        close=close,
        adj_close=None,
        volume=1000.0,
        value=close * 1000.0,
        source=source,
    )


class TestUpsertRows:
    @pytest.mark.asyncio
    async def test_upsert_groups_payload_per_table_config(self):
        session = MagicMock()
        session.execute = AsyncMock()
        repo = DailyCandlesRepository(session=session)

        rows = [
            _row("AAPL", "NASD", datetime(2026, 5, 14, tzinfo=UTC), 150.0),
            _row("MSFT", "NASD", datetime(2026, 5, 14, tzinfo=UTC), 400.0),
        ]
        await repo.upsert_rows(market=MarketKey.US, rows=rows)

        assert session.execute.await_count == 1
        args, kwargs = session.execute.await_args
        payload = args[1]
        assert len(payload) == 2
        assert {p["symbol"] for p in payload} == {"AAPL", "MSFT"}
        assert all(p["exchange"] == "NASD" for p in payload)
        assert all(p["source"] == "kis" for p in payload)
        assert all("adj_close" in p for p in payload)

    @pytest.mark.asyncio
    async def test_upsert_skips_when_empty(self):
        session = MagicMock()
        session.execute = AsyncMock()
        repo = DailyCandlesRepository(session=session)

        result = await repo.upsert_rows(market=MarketKey.KR, rows=[])

        assert result == 0
        session.execute.assert_not_awaited()


class TestUpsertCryptoRowsIdentityDedupe:
    @pytest.mark.asyncio
    async def test_resolves_instrument_id_once_per_identity_not_per_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROB-830 defect 3 regression.

        ``identities_by_pair.setdefault(pair, await resolver(...))`` always
        evaluates the default argument first, so the resolver ran once per
        *row* even though the dict only ever kept one entry per identity.
        Three rows sharing one (symbol, partition) identity must resolve
        exactly once.
        """
        session = MagicMock()
        session.execute = AsyncMock()
        repo = DailyCandlesRepository(session=session)

        resolve = AsyncMock(return_value=7)
        upsert_by_id = AsyncMock(return_value=1)
        monkeypatch.setattr(repo, "_resolve_instrument_id", resolve)
        monkeypatch.setattr(repo, "upsert_crypto_rows_by_instrument_id", upsert_by_id)

        rows = [
            _row("KRW-BTC", "upbit_krw", datetime(2026, 5, 12, tzinfo=UTC), 100.0),
            _row("KRW-BTC", "upbit_krw", datetime(2026, 5, 13, tzinfo=UTC), 101.0),
            _row("KRW-BTC", "upbit_krw", datetime(2026, 5, 14, tzinfo=UTC), 102.0),
        ]

        total = await repo.upsert_rows(market=MarketKey.CRYPTO, rows=rows)

        resolve.assert_awaited_once_with(symbol="KRW-BTC", partition="upbit_krw")
        upsert_by_id.assert_awaited_once()
        _, kwargs = upsert_by_id.await_args
        assert kwargs["instrument_id"] == 7
        assert len(kwargs["rows"]) == 3
        assert total == 1


class TestFetchRange:
    @pytest.mark.asyncio
    async def test_fetch_range_binds_window_and_partition(self):
        session = MagicMock()
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)
        repo = DailyCandlesRepository(session=session)

        start = datetime(2026, 6, 1, tzinfo=UTC)
        end = datetime(2026, 6, 30, tzinfo=UTC)
        rows = await repo.fetch_range(
            market=MarketKey.KR,
            symbol="005930",
            partition="KRX",
            start=start,
            end=end,
        )

        assert rows == []
        assert session.execute.await_count == 1
        args, _ = session.execute.await_args
        sql = str(args[0])
        params = args[1]
        assert "time >= :start AND time <= :end" in sql
        assert "ORDER BY time ASC" in sql
        assert params == {
            "symbol": "005930",
            "partition": "KRX",
            "start": start,
            "end": end,
        }
