"""ROB-116 — PortfolioActionService aggregation tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.portfolio_action_service import PortfolioActionService


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregates_holdings_with_research_and_journal(monkeypatch) -> None:
    db = MagicMock()
    user_id = 1

    h = MagicMock(
        ticker="KRW-SOL",
        market_type="crypto",
        quantity=10.0,
        evaluation=2_975_000.0,
        profit_loss_rate=None,
        profit_rate=-0.1225,
        current_price=297_500.0,
        instrument_type="crypto",
    )
    h.name = "솔라나"
    holdings = [h]

    service = PortfolioActionService(db)
    monkeypatch.setattr(
        service, "_load_holdings", AsyncMock(return_value=(holdings, 10_000_000.0, []))
    )
    monkeypatch.setattr(
        service,
        "_load_latest_summary",
        AsyncMock(
            return_value={
                "session_id": 32,
                "decision": "hold",
                "confidence": 55,
                "market_verdict": "neutral",
                "nearest_support_pct": -1.93,
                "nearest_resistance_pct": 1.22,
            }
        ),
    )
    monkeypatch.setattr(
        service, "_load_journal_status", AsyncMock(return_value="missing")
    )

    result = await service.build_action_board(user_id=user_id)

    assert result.total == 1
    candidate = result.candidates[0]
    assert candidate.symbol == "KRW-SOL"
    assert candidate.market == "CRYPTO"
    assert candidate.profit_rate == -0.1225
    assert candidate.latest_research_session_id == 32
    assert candidate.summary_decision == "hold"
    assert candidate.candidate_action in {"trim", "hold", "watch"}
    assert "journal_missing" in candidate.missing_context_codes


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skips_zero_quantity_holdings(monkeypatch) -> None:
    db = MagicMock()
    holdings = [MagicMock(ticker="ZERO", quantity=0.0)]
    service = PortfolioActionService(db)
    monkeypatch.setattr(
        service, "_load_holdings", AsyncMock(return_value=(holdings, 0.0, []))
    )

    result = await service.build_action_board(user_id=1)
    assert result.total == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_holdings_keeps_crypto_when_kis_unavailable(monkeypatch) -> None:
    db = MagicMock()
    crypto_holding = MagicMock(ticker="KRW-BTC", quantity=1.0, evaluation=100.0)
    service = PortfolioActionService(db)
    monkeypatch.setattr(
        service, "_load_crypto_holdings", AsyncMock(return_value=[crypto_holding])
    )

    def raise_kis_error():
        raise RuntimeError("KIS config unavailable")

    monkeypatch.setattr("app.services.brokers.kis.KISClient", raise_kis_error)

    holdings, total, warnings = await service._load_holdings(1, None)

    assert holdings == [crypto_holding]
    assert total == 100.0
    assert any(w.startswith("KR holdings unavailable") for w in warnings)
    assert any(w.startswith("US holdings unavailable") for w in warnings)
