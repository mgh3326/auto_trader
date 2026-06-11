from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

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


def _session_factory_noop():
    return _NoopSession()


@pytest.fixture
def session_factory_noop():
    return _session_factory_noop


@pytest.fixture
def session_factory():
    from app.core.db import AsyncSessionLocal

    return AsyncSessionLocal


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_snapshot_page_returns_empty_page_summary(session_factory_noop):
    assert await enrich_snapshot_page(
        rows=[],
        market="kr",
        session_factory=session_factory_noop,
    ) == {
        "results": [],
        "summary": {
            "attempted": 0,
            "consensusSucceeded": 0,
            "rsiSucceeded": 0,
            "sectorResolved": 0,
            "warnings": [],
        },
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_snapshot_page_adds_consensus_rsi_and_summary(
    monkeypatch, session_factory_noop
):
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

    # ROB-512: mock sector labels to avoid DB lookup failure in no-op session
    async def _fake_sector(**kwargs):
        return {}

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment._sector_labels_for_page",
        _fake_sector,
    )

    out = await enrich_snapshot_page(
        rows=[
            {"symbol": "005930", "analystLabel": "-"},
            {"symbol": "000660", "analystLabel": "-"},
        ],
        market="kr",
        session_factory=session_factory_noop,
        opinion_provider=_fake_opinions,
    )

    assert out["summary"] == {
        "attempted": 2,
        "consensusSucceeded": 1,
        "rsiSucceeded": 1,
        "sectorResolved": 0,
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
async def test_enrich_snapshot_page_fails_open_when_rsi_lookup_errors(
    monkeypatch, session_factory_noop
):
    async def _broken_rsi(db, *, market: str, symbols: list[str]) -> dict[str, float]:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment._rsi_by_symbol",
        _broken_rsi,
    )

    # ROB-512: mock sector labels to avoid DB lookup failure in no-op session
    async def _fake_sector(**kwargs):
        return {}

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment._sector_labels_for_page",
        _fake_sector,
    )

    out = await enrich_snapshot_page(
        rows=[{"symbol": "BTC", "analystLabel": "-"}],
        market="crypto",
        session_factory=session_factory_noop,
    )

    assert out["summary"] == {
        "attempted": 1,
        "consensusSucceeded": 0,
        "rsiSucceeded": 0,
        "sectorResolved": 0,
        "warnings": ["rsi_enrichment_unavailable"],
    }
    assert out["results"][0]["analysisContext"] == {
        "consensus": None,
        "rsi14": None,
        "dataState": "missing",
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# ROB-512 갭3: sector lazy fill
# ---------------------------------------------------------------------------

_SECTOR_TEST_KR_SYMBOL = "919100"


@pytest_asyncio.fixture
async def _sector_clean(db_session):
    import sqlalchemy as sa

    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.models.symbol_sectors import SymbolSector

    async def _purge():
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol == _SECTOR_TEST_KR_SYMBOL
            )
        )
        await db_session.execute(
            sa.delete(SymbolSector).where(SymbolSector.source_key.like("999%"))
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


@pytest.mark.asyncio
async def test_sector_lazy_fill_kr_persists_and_replaces_category(
    db_session, session_factory, _sector_clean
):
    """NULL sector 심볼 → fake fetch로 (업종번호, 한글명) 획득 → persist →
    응답 category '-'가 한글로 교체. 두 번째 호출은 fetch 0 (DB hit)."""
    import sqlalchemy as sa

    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.services.invest_view_model.screener_analysis_enrichment import (
        enrich_snapshot_page,
    )

    db_session.add(
        KRSymbolUniverse(
            symbol=_SECTOR_TEST_KR_SYMBOL,
            name="테스트",
            exchange="KOSPI",
            is_active=True,
        )
    )
    await db_session.commit()

    calls: list[str] = []

    async def fake_fetch_kr(code: str):
        calls.append(code)
        return "999278", "반도체와반도체장비"

    rows = [{"symbol": _SECTOR_TEST_KR_SYMBOL, "market": "kr", "category": "-"}]

    async def no_opinions(**kwargs):
        return {"error": "analyst_consensus_unavailable"}

    out1 = await enrich_snapshot_page(
        rows=rows,
        market="kr",
        session_factory=session_factory,
        opinion_provider=no_opinions,
        fetch_kr_sector=fake_fetch_kr,
    )
    assert calls == [_SECTOR_TEST_KR_SYMBOL]
    assert out1["results"][0]["category"] == "반도체와반도체장비"
    assert out1["summary"]["sectorResolved"] == 1

    # persist 확인 + 2회차 fetch 0
    row = (
        await db_session.execute(
            sa.select(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol == _SECTOR_TEST_KR_SYMBOL
            )
        )
    ).scalar_one()
    await db_session.refresh(row)
    assert row.sector_id is not None

    out2 = await enrich_snapshot_page(
        rows=rows,
        market="kr",
        session_factory=session_factory,
        opinion_provider=no_opinions,
        fetch_kr_sector=fake_fetch_kr,
    )
    assert calls == [_SECTOR_TEST_KR_SYMBOL]  # 추가 fetch 없음
    assert out2["results"][0]["category"] == "반도체와반도체장비"


@pytest.mark.asyncio
async def test_sector_lazy_fill_fails_open(db_session, session_factory, _sector_clean):
    """fetch 실패 → category 유지('-'), 워닝 없이도 결과 자체는 정상."""
    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.services.invest_view_model.screener_analysis_enrichment import (
        enrich_snapshot_page,
    )

    db_session.add(
        KRSymbolUniverse(
            symbol=_SECTOR_TEST_KR_SYMBOL,
            name="테스트",
            exchange="KOSPI",
            is_active=True,
        )
    )
    await db_session.commit()

    async def boom(code: str):
        raise RuntimeError("naver down")

    async def no_opinions(**kwargs):
        return {"error": "analyst_consensus_unavailable"}

    out = await enrich_snapshot_page(
        rows=[{"symbol": _SECTOR_TEST_KR_SYMBOL, "market": "kr", "category": "-"}],
        market="kr",
        session_factory=session_factory,
        opinion_provider=no_opinions,
        fetch_kr_sector=boom,
    )
    assert out["results"][0]["category"] == "-"
    assert out["summary"]["sectorResolved"] == 0
