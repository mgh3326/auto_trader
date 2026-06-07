from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.services.invest_crypto_screener_snapshots.builder import (
    CryptoProviderRow,
    build_crypto_snapshot_payloads,
    build_crypto_snapshots,
)
from app.services.invest_crypto_screener_snapshots.provider import (
    TvScreenerUpbitCryptoSnapshotProvider,
)
from app.services.invest_crypto_screener_snapshots.repository import (
    CryptoSnapshotUpsert,
)


@pytest.fixture(autouse=True)
def _stub_funding(monkeypatch):
    # ROB-443: build_crypto_snapshots enriches via Binance; stub it so unit tests
    # never hit the network. Individual tests override with their own map.
    import app.services.invest_crypto_screener_snapshots.builder as builder_mod

    async def _empty(_symbols):
        return {}

    monkeypatch.setattr(builder_mod, "fetch_funding_rates", _empty)
    monkeypatch.setattr(builder_mod, "fetch_oi_and_long_short", _empty)


class _Condition:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return isinstance(other, _Condition) and self.label == other.label


class _Field:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> _Condition:  # type: ignore[override]
        return _Condition(f"{self.label}=={other}")


class _Provider:
    async def fetch_rows(self, *, limit: int | None = None) -> list[CryptoProviderRow]:
        return [
            CryptoProviderRow(
                symbol="KRW-BTC",
                name="비트코인",
                latest_close=Decimal("150000000"),
                change_rate=Decimal("2.5"),
                trade_amount_24h=Decimal("120000000000"),
                volume_24h=Decimal("123.456"),
                volume_24h_usd=Decimal("90000000"),
                market_cap=Decimal("2100000000000"),
                rsi=Decimal("31.5"),
                adx=Decimal("24.1"),
                market_warning=False,
                raw_payload={"provider": "fixture"},
            )
        ][:limit]


class _Repo:
    def __init__(self) -> None:
        self.payloads: list[CryptoSnapshotUpsert] = []

    async def upsert(self, payload: CryptoSnapshotUpsert) -> None:
        self.payloads.append(payload)


def test_build_crypto_snapshot_payloads_maps_provider_rows() -> None:
    payloads = build_crypto_snapshot_payloads(
        [
            CryptoProviderRow(
                symbol="KRW-BTC",
                name="Bitcoin",
                latest_close=Decimal("150000000"),
                change_rate=Decimal("2.5"),
                trade_amount_24h=Decimal("120000000000"),
                volume_24h=Decimal("123.456"),
                market_cap=Decimal("2100000000000"),
                rsi=Decimal("31.5"),
                adx=Decimal("24.1"),
                market_warning=True,
                raw_payload={"safe": True},
            )
        ],
        snapshot_date=dt.date(2026, 5, 13),
    )

    payload = payloads[0]
    assert payload.symbol == "KRW-BTC"
    assert payload.snapshot_date == dt.date(2026, 5, 13)
    assert payload.source == "tvscreener_upbit"
    assert payload.trade_amount_24h == Decimal("120000000000")
    assert payload.volume_24h == Decimal("123.456")
    assert payload.rsi == Decimal("31.5")
    assert payload.adx == Decimal("24.1")
    assert payload.market_warning is True
    assert payload.raw_payload == {"safe": True}


def test_build_crypto_snapshot_payloads_sets_funding_rate() -> None:
    # ROB-443: funding map enriches matching coins; absent coins stay None.
    payloads = build_crypto_snapshot_payloads(
        [
            CryptoProviderRow(symbol="KRW-BTC", latest_close=Decimal("150000000")),
            CryptoProviderRow(symbol="KRW-NOPERP", latest_close=Decimal("100")),
        ],
        snapshot_date=dt.date(2026, 5, 13),
        funding_by_symbol={"KRW-BTC": Decimal("0.00012")},
    )
    by_symbol = {p.symbol: p for p in payloads}
    assert by_symbol["KRW-BTC"].funding_rate == Decimal("0.00012")
    assert by_symbol["KRW-NOPERP"].funding_rate is None  # no perp → fail-closed


@pytest.mark.asyncio
async def test_build_crypto_snapshots_enriches_funding(monkeypatch) -> None:
    import app.services.invest_crypto_screener_snapshots.builder as builder_mod

    async def _funding(symbols):
        assert symbols == ["KRW-BTC"]  # provider symbols passed through
        return {"KRW-BTC": Decimal("0.0005")}

    monkeypatch.setattr(builder_mod, "fetch_funding_rates", _funding)
    repo = _Repo()
    result = await build_crypto_snapshots(
        provider=_Provider(),
        repository=repo,
        snapshot_date=dt.date(2026, 5, 13),
        commit=True,
        limit=1,
    )
    assert result["fundingEnriched"] == 1
    assert repo.payloads[0].funding_rate == Decimal("0.0005")


@pytest.mark.asyncio
async def test_build_crypto_snapshots_dry_run_does_not_upsert() -> None:
    repo = _Repo()

    result = await build_crypto_snapshots(
        provider=_Provider(),
        repository=repo,
        snapshot_date=dt.date(2026, 5, 13),
        commit=False,
        limit=1,
    )

    assert result["snapshot_date"] == "2026-05-13"
    assert result["fetched"] == 1
    assert result["would_upsert"] == 1
    assert result["upserted"] == 0
    assert repo.payloads == []


@pytest.mark.asyncio
async def test_build_crypto_snapshots_commit_upserts_payloads() -> None:
    repo = _Repo()

    result = await build_crypto_snapshots(
        provider=_Provider(),
        repository=repo,
        snapshot_date=dt.date(2026, 5, 13),
        commit=True,
        limit=1,
    )

    assert result["upserted"] == 1
    assert repo.payloads[0].symbol == "KRW-BTC"


@pytest.mark.asyncio
async def test_tvscreener_upbit_snapshot_provider_keeps_cooldown_symbols() -> None:
    service = AsyncMock()
    service.query_crypto_screener.return_value = pd.DataFrame(
        {
            "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
            "name": ["BTCKRW", "ETHKRW", "XRPKRW"],
            "description": ["Bitcoin", "Ethereum", "XRP"],
            "price": [150_000_000.0, 5_000_000.0, 3_000.0],
            "change_percent": [1.5, -0.2, -25.0],
            "relative_strength_index_14": [45.5, 32.1, 28.5],
            "average_directional_index_14": [25.3, 18.7, 30.0],
            "value_traded": [
                900_000_000_000.0,
                1_200_000_000_000.0,
                700_000_000_000.0,
            ],
            "market_cap": [
                2_500_000_000_000_000.0,
                1_200_000_000_000_000.0,
                500_000_000_000_000.0,
            ],
            "volume_24h_in_usd": [156_000_000.0, 95_000_000.0, 44_000_000.0],
            "exchange": ["UPBIT", "UPBIT", "UPBIT"],
        }
    )
    fake_tvscreener = type(
        "FakeTvScreener",
        (),
        {
            "CryptoField": type(
                "CryptoField",
                (),
                {
                    "NAME": _Field("name"),
                    "DESCRIPTION": _Field("description"),
                    "PRICE": _Field("price"),
                    "CHANGE_PERCENT": _Field("change_percent"),
                    "VALUE_TRADED": _Field("value_traded"),
                    "MARKET_CAP": _Field("market_cap"),
                    "RELATIVE_STRENGTH_INDEX_14": _Field("rsi14"),
                    "AVERAGE_DIRECTIONAL_INDEX_14": _Field("adx14"),
                    "VOLUME_24H_IN_USD": _Field("volume_usd"),
                    "EXCHANGE": _Field("exchange"),
                },
            )
        },
    )
    fetch_multiple_tickers = AsyncMock(
        return_value=[
            {"market": "KRW-BTC", "acc_trade_volume_24h": 15_600.0},
            {"market": "KRW-ETH", "acc_trade_volume_24h": 9_500.0},
            {"market": "KRW-XRP", "acc_trade_volume_24h": 4_400.0},
        ]
    )
    cooldown_filter = AsyncMock(return_value={"KRW-ETH"})

    with (
        patch(
            "app.mcp_server.tooling.screening.crypto._import_tvscreener",
            return_value=fake_tvscreener,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.upbit_service.fetch_multiple_tickers",
            new=fetch_multiple_tickers,
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.get_upbit_market_display_names",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto.get_upbit_warning_markets",
            new=AsyncMock(return_value={"KRW-ETH"}),
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto._run_crypto_coingecko_fetch",
            new=AsyncMock(
                return_value={
                    "data": {},
                    "cached": True,
                    "age_seconds": 0.0,
                    "stale": False,
                    "error": None,
                }
            ),
        ),
        patch(
            "app.mcp_server.tooling.screening.crypto._get_crypto_trade_cooldown_service",
            return_value=type(
                "Cooldown", (), {"filter_symbols_in_cooldown": cooldown_filter}
            )(),
        ),
    ):
        rows = await TvScreenerUpbitCryptoSnapshotProvider().fetch_rows(limit=10)

    assert cooldown_filter.await_count == 0
    assert {row.symbol for row in rows} == {"KRW-BTC", "KRW-ETH", "KRW-XRP"}
