from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.daily_candles.repository import MarketKey
from app.services.daily_candles.sync_service import SyncTarget


def test_cli_argument_parser_accepts_required_args():
    from scripts.backfill_daily_candles import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(
        ["--market", "us", "--symbols", "AAPL,MSFT", "--horizon-bars", "500"]
    )
    assert ns.market == "us"
    assert ns.symbols == "AAPL,MSFT"
    assert ns.horizon_bars == 500
    assert ns.dry_run is False


def test_cli_dry_run_flag():
    from scripts.backfill_daily_candles import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(["--market", "kr", "--symbols", "005930", "--dry-run"])
    assert ns.dry_run is True


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("KRW-BTC", "upbit_krw"),
        ("USDT-ETH", "upbit_usdt"),
    ],
)
def test_crypto_partition_is_derived_from_symbol(symbol: str, expected: str) -> None:
    from scripts.backfill_daily_candles import _partition_for_symbol

    assert (
        _partition_for_symbol(
            market=MarketKey.CRYPTO,
            symbol=symbol,
            requested_partition=None,
        )
        == expected
    )


def test_crypto_partition_override_must_match_symbol() -> None:
    from scripts.backfill_daily_candles import _partition_for_symbol

    with pytest.raises(ValueError, match="must match"):
        _partition_for_symbol(
            market=MarketKey.CRYPTO,
            symbol="USDT-ETH",
            requested_partition="upbit_krw",
        )


@pytest.mark.asyncio
async def test_crypto_backfill_builds_symbol_specific_canonical_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.backfill_daily_candles as cli

    service = SimpleNamespace(
        sync_one=AsyncMock(
            return_value=SimpleNamespace(rows_upserted=1, fallback_used=False)
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(cli, "_build_default_service", AsyncMock(return_value=service))
    args = cli._build_parser().parse_args(
        ["--market", "crypto", "--symbols", "KRW-BTC,USDT-ETH"]
    )

    assert await cli._amain(args) == 0
    assert [call.kwargs["target"] for call in service.sync_one.await_args_list] == [
        SyncTarget(
            market=MarketKey.CRYPTO,
            symbol="KRW-BTC",
            partition="upbit_krw",
        ),
        SyncTarget(
            market=MarketKey.CRYPTO,
            symbol="USDT-ETH",
            partition="upbit_usdt",
        ),
    ]
    service.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbols",
    [
        "KRW-BTC,USDT-ETH",
        "KRW-BTC,KRW-BTC/USD",
    ],
)
async def test_crypto_backfill_preflights_all_targets_before_service_or_sync(
    monkeypatch: pytest.MonkeyPatch,
    symbols: str,
) -> None:
    import scripts.backfill_daily_candles as cli

    service_factory = AsyncMock()
    monkeypatch.setattr(cli, "_build_default_service", service_factory)
    args = cli._build_parser().parse_args(
        [
            "--market",
            "crypto",
            "--symbols",
            symbols,
            "--partition",
            "upbit_krw",
        ]
    )

    with pytest.raises(ValueError):
        await cli._amain(args)

    service_factory.assert_not_awaited()
