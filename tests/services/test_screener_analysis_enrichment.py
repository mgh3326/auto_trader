from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from app.models.invest_crypto_screener_snapshot import InvestCryptoScreenerSnapshot
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_view_model.screener_analysis_enrichment import (
    _opinion_payload,
    _rsi_by_symbol,
    build_analyst_label,
    build_rsi14_from_closes,
    enrich_snapshot_page,
    normalize_consensus_payload,
)


@pytest.mark.unit
def test_build_rsi14_from_closes_returns_latest_rsi():
    closes = [
        100,
        101,
        102,
        101,
        103,
        104,
        103,
        105,
        106,
        107,
        106,
        108,
        109,
        110,
        111,
    ]

    assert build_rsi14_from_closes(closes) == pytest.approx(84.07, abs=0.01)


@pytest.mark.unit
def test_build_rsi14_from_closes_ignores_invalid_short_windows():
    assert build_rsi14_from_closes(["bad", "", None, 100]) is None


@pytest.mark.unit
def test_normalize_consensus_payload_maps_snake_to_camel():
    payload: dict[str, Any] = {
        "source": "naver",
        "consensus": {
            "buy_count": 2,
            "hold_count": 1,
            "sell_count": 0,
            "strong_buy_count": 0,
            "total_count": 3,
            "avg_target_price": 78500,
            "median_target_price": 78000,
            "min_target_price": 76000,
            "max_target_price": 81000,
            "upside_pct": 12.3,
            "current_price": 69900,
        },
    }

    consensus, warnings = normalize_consensus_payload(payload)

    assert warnings == []
    assert consensus is not None
    assert consensus.buyCount == 2
    assert consensus.avgTargetPrice == pytest.approx(78500)
    assert consensus.upsidePct == pytest.approx(12.3)


@pytest.mark.unit
def test_normalize_consensus_payload_rejects_malformed_consensus():
    consensus, warnings = normalize_consensus_payload({"consensus": ["bad"]})
    assert consensus is None
    assert warnings == ["analyst_consensus_unavailable"]

    consensus, warnings = normalize_consensus_payload({"consensus": {}})
    assert consensus is None
    assert warnings == ["analyst_consensus_missing"]


@pytest.mark.unit
def test_build_analyst_label_uses_counts_and_upside():
    payload: dict[str, Any] = {
        "source": "naver",
        "consensus": {
            "buy_count": 2,
            "hold_count": 1,
            "sell_count": 0,
            "total_count": 3,
            "upside_pct": 12.34,
        },
    }
    consensus, warnings = normalize_consensus_payload(payload)

    assert warnings == []
    assert build_analyst_label(consensus) == "매수 2 / 보유 1 / 매도 0 · 목표 +12.3%"


@pytest.mark.unit
def test_build_analyst_label_omits_target_when_upside_missing():
    consensus, warnings = normalize_consensus_payload(
        {
            "source": "naver",
            "consensus": {
                "buy_count": 1,
                "hold_count": 2,
                "sell_count": 0,
                "total_count": 3,
            },
        }
    )

    assert warnings == []
    assert build_analyst_label(consensus) == "매수 1 / 보유 2 / 매도 0"


@pytest.mark.unit
def test_consensus_sanity_warning_when_bullish_target_below_current():
    payload: dict[str, Any] = {
        "source": "naver",
        "consensus": {
            "buy_count": 3,
            "hold_count": 0,
            "sell_count": 0,
            "total_count": 3,
            "avg_target_price": 90000,
            "current_price": 100000,
            "upside_pct": 15.0,
        },
    }

    consensus, warnings = normalize_consensus_payload(payload)

    assert consensus is not None
    assert "consensus_target_below_current_with_bullish_votes" in warnings
    assert "consensus_upside_mismatch" in warnings
    assert build_analyst_label(consensus, warnings=warnings) == "컨센 확인필요"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_opinion_payload_handles_sync_timeout_and_provider_errors():
    def _sync_provider(*, symbol: str, market: str, limit: int):
        return {"symbol": symbol, "market": market, "limit": limit}

    async def _timeout_provider(**kwargs):
        raise TimeoutError

    def _failing_provider(**kwargs):
        raise RuntimeError("provider unavailable")

    assert await _opinion_payload(_sync_provider, symbol="AAPL", market="us") == {
        "symbol": "AAPL",
        "market": "us",
        "limit": 10,
    }
    assert await _opinion_payload(_timeout_provider, symbol="005930", market="kr") == {
        "error": "analyst_consensus_timeout"
    }
    assert await _opinion_payload(_failing_provider, symbol="AAPL", market="us") == {
        "error": "analyst_consensus_unavailable"
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rsi_by_symbol_reads_equity_and_crypto_snapshots(db_session):
    equity_date = dt.date(2099, 12, 10)
    crypto_date = dt.date(2099, 12, 11)
    await db_session.execute(
        InvestScreenerSnapshot.__table__.delete().where(
            InvestScreenerSnapshot.snapshot_date == equity_date
        )
    )
    await db_session.execute(
        InvestCryptoScreenerSnapshot.__table__.delete().where(
            InvestCryptoScreenerSnapshot.snapshot_date == crypto_date
        )
    )
    db_session.add_all(
        [
            InvestScreenerSnapshot(
                market="kr",
                symbol="005930",
                snapshot_date=equity_date,
                latest_close=Decimal("111"),
                change_rate=Decimal("1.0"),
                closes_window=[
                    100,
                    101,
                    102,
                    101,
                    103,
                    104,
                    103,
                    105,
                    106,
                    107,
                    106,
                    108,
                    109,
                    110,
                    111,
                ],
                source="kis",
            ),
            InvestScreenerSnapshot(
                market="kr",
                symbol="000660",
                snapshot_date=equity_date,
                latest_close=Decimal("100"),
                change_rate=Decimal("1.0"),
                closes_window=[100],
                source="kis",
            ),
            InvestCryptoScreenerSnapshot(
                symbol="KRW-BTC",
                snapshot_date=crypto_date,
                latest_close=Decimal("100"),
                rsi=Decimal("61.2500"),
                source="tvscreener_upbit",
            ),
            InvestCryptoScreenerSnapshot(
                symbol="KRW-ETH",
                snapshot_date=crypto_date,
                latest_close=Decimal("100"),
                rsi=None,
                source="tvscreener_upbit",
            ),
        ]
    )
    await db_session.commit()

    assert await _rsi_by_symbol(db_session, market="kr", symbols=[]) == {}
    assert await _rsi_by_symbol(db_session, market="us", symbols=["NOPE"]) == {}
    assert await _rsi_by_symbol(db_session, market="crypto", symbols=["KRW-NONE"]) == {}
    assert await _rsi_by_symbol(db_session, market="unknown", symbols=["005930"]) == {}
    assert await _rsi_by_symbol(
        db_session, market="kr", symbols=["005930", "000660"]
    ) == {"005930": pytest.approx(84.07, abs=0.01)}
    assert await _rsi_by_symbol(
        db_session, market="crypto", symbols=["KRW-BTC", "KRW-ETH"]
    ) == {"KRW-BTC": pytest.approx(61.25)}

    await db_session.execute(
        InvestScreenerSnapshot.__table__.delete().where(
            InvestScreenerSnapshot.snapshot_date == equity_date
        )
    )
    await db_session.execute(
        InvestCryptoScreenerSnapshot.__table__.delete().where(
            InvestCryptoScreenerSnapshot.snapshot_date == crypto_date
        )
    )
    await db_session.commit()


class _NoopSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _session_factory():
    return _NoopSession()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_snapshot_page_returns_empty_page_summary():
    assert await enrich_snapshot_page(
        rows=[],
        market="kr",
        session_factory=_session_factory,
    ) == {
        "results": [],
        "summary": {
            "attempted": 0,
            "consensusSucceeded": 0,
            "rsiSucceeded": 0,
            "warnings": [],
        },
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_snapshot_page_adds_consensus_rsi_and_summary(monkeypatch):
    async def _fake_rsi(db, *, market: str, symbols: list[str]) -> dict[str, float]:
        assert market == "kr"
        assert symbols == ["005930", "000660"]
        return {"005930": 58.42}

    async def _fake_opinions(*, symbol: str, market: str, limit: int):
        assert market == "kr"
        assert limit == 10
        if symbol == "005930":
            return {
                "source": "naver",
                "consensus": {
                    "buy_count": 2,
                    "hold_count": 1,
                    "sell_count": 0,
                    "total_count": 3,
                    "upside_pct": 12.34,
                },
            }
        return {"error": "not_found"}

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment._rsi_by_symbol",
        _fake_rsi,
    )

    out = await enrich_snapshot_page(
        rows=[
            {"symbol": "005930", "analystLabel": "-"},
            {"symbol": "000660", "analystLabel": "-"},
        ],
        market="kr",
        session_factory=_session_factory,
        opinion_provider=_fake_opinions,
    )

    assert out["summary"] == {
        "attempted": 2,
        "consensusSucceeded": 1,
        "rsiSucceeded": 1,
        "warnings": [],
    }
    first, second = out["results"]
    assert first["analystLabel"] == "매수 2 / 보유 1 / 매도 0 · 목표 +12.3%"
    assert first["analysisContext"]["dataState"] == "fresh"
    assert first["analysisContext"]["rsi14"] == pytest.approx(58.42)
    assert second["analystLabel"] == "-"
    assert second["analysisContext"]["dataState"] == "missing"
    assert second["analysisContext"]["warnings"] == ["analyst_consensus_unavailable"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_snapshot_page_fails_open_when_rsi_lookup_errors(monkeypatch):
    async def _broken_rsi(db, *, market: str, symbols: list[str]) -> dict[str, float]:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment._rsi_by_symbol",
        _broken_rsi,
    )

    out = await enrich_snapshot_page(
        rows=[{"symbol": "BTC", "analystLabel": "-"}],
        market="crypto",
        session_factory=_session_factory,
    )

    assert out["summary"] == {
        "attempted": 1,
        "consensusSucceeded": 0,
        "rsiSucceeded": 0,
        "warnings": ["rsi_enrichment_unavailable"],
    }
    assert out["results"][0]["analysisContext"] == {
        "consensus": None,
        "rsi14": None,
        "dataState": "missing",
        "warnings": [],
    }
