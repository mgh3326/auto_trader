"""ROB-117 — Candidate screening service tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.candidate_screening_service import CandidateScreeningService


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wraps_screen_stocks_and_annotates_held(monkeypatch) -> None:
    fake_screen = AsyncMock(
        return_value={
            "stocks": [
                {
                    "symbol": "KRW-BTC",
                    "name": "비트코인",
                    "trade_amount_24h": 0.0,
                    "volume_ratio": None,
                    "rsi": 28.5,
                    "warnings": ["KRW-BTC ticker not found"],
                },
                {"symbol": "KRW-ETH", "name": "이더리움", "rsi": 32.1},
            ],
            "rsi_enrichment": {"attempted": 0, "succeeded": 0},
            "warnings": ["rsi_enrichment_skipped"],
        }
    )
    monkeypatch.setattr(
        "app.services.candidate_screening_service.screen_stocks_impl", fake_screen
    )

    db = MagicMock()
    service = CandidateScreeningService(db)
    monkeypatch.setattr(
        service, "_load_held_symbols", AsyncMock(return_value={"KRW-BTC"})
    )

    res = await service.screen(
        user_id=1, market="crypto", strategy="oversold", sort_by=None, limit=10
    )

    assert res.total == 2
    btc = next(c for c in res.candidates if c.symbol == "KRW-BTC")
    eth = next(c for c in res.candidates if c.symbol == "KRW-ETH")
    assert btc.is_held is True
    assert eth.is_held is False
    assert "rsi_enrichment_skipped" in res.warnings
    assert any("KRW-BTC" in w for w in btc.data_warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_passes_filters_through(monkeypatch) -> None:
    fake_screen = AsyncMock(return_value={"stocks": [], "warnings": []})
    monkeypatch.setattr(
        "app.services.candidate_screening_service.screen_stocks_impl", fake_screen
    )

    service = CandidateScreeningService(MagicMock())
    monkeypatch.setattr(service, "_load_held_symbols", AsyncMock(return_value=set()))

    await service.screen(
        user_id=1,
        market="kr",
        strategy="momentum",
        sort_by="change_rate",
        limit=20,
        max_per=15.0,
        adv_krw_min=1_000_000_000,
    )
    fake_screen.assert_awaited_once()
    kwargs = fake_screen.await_args.kwargs
    assert kwargs["market"] == "kr"
    assert kwargs["strategy"] == "momentum"
    assert kwargs["sort_by"] == "change_rate"
    assert kwargs["limit"] == 20
    assert kwargs["max_per"] == 15.0
    assert kwargs["adv_krw_min"] == 1_000_000_000
