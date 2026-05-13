from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.routers import invest_api
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import router as invest_api_router
from app.schemas.invest_common_preferred_disparity import (
    CommonPreferredDisparityCard,
    CommonPreferredDisparityResponse,
    DisparitySource,
)
from app.services.invest_view_model.common_preferred_disparity_service import (
    build_common_preferred_disparity,
)
from app.services.invest_view_model.kr_preferred_pairs import (
    KRSymbolRow,
    discover_common_preferred_pairs,
)


def test_seed_pair_discovers_samsung_common_preferred() -> None:
    pairs = discover_common_preferred_pairs(
        [
            KRSymbolRow(symbol="005930", name="삼성전자", exchange="KOSPI"),
            KRSymbolRow(symbol="005935", name="삼성전자우", exchange="KOSPI"),
        ],
        symbols={"005930"},
    )

    assert [(p.common_symbol, p.preferred_symbol) for p in pairs] == [("005930", "005935")]
    assert pairs[0].mapping_source == "heuristic_name_suffix"


@pytest.mark.asyncio
async def test_build_common_preferred_disparity_calculates_discount_and_zscore(db_session) -> None:
    now = dt.datetime(2026, 5, 14, 6, 0, tzinfo=dt.UTC)
    common_symbol = "901001"
    preferred_symbol = "901002"
    await db_session.execute(
        sa.delete(MarketQuoteSnapshot).where(
            MarketQuoteSnapshot.symbol.in_([common_symbol, preferred_symbol])
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_([common_symbol, preferred_symbol]))
    )
    await db_session.commit()
    db_session.add_all(
        [
            KRSymbolUniverse(symbol=common_symbol, name="테스트홀딩", exchange="KOSPI", is_active=True),
            KRSymbolUniverse(symbol=preferred_symbol, name="테스트홀딩우", exchange="KOSPI", is_active=True),
        ]
    )
    prices = [
        (now - dt.timedelta(days=2), Decimal("100000"), Decimal("82000")),
        (now - dt.timedelta(days=1), Decimal("100000"), Decimal("80000")),
        (now, Decimal("100000"), Decimal("78000")),
    ]
    rows = []
    row_id = 2500000
    for snapshot_at, common_price, preferred_price in prices:
        rows.extend(
            [
                MarketQuoteSnapshot(
                    id=row_id,
                    market="kr",
                    symbol=common_symbol,
                    source="kis",
                    snapshot_at=snapshot_at,
                    price=common_price,
                ),
                MarketQuoteSnapshot(
                    id=row_id + 1,
                    market="kr",
                    symbol=preferred_symbol,
                    source="kis",
                    snapshot_at=snapshot_at,
                    price=preferred_price,
                ),
            ]
        )
        row_id += 2
    db_session.add_all(rows)
    await db_session.commit()

    response = await build_common_preferred_disparity(
        db=db_session,
        symbols=[common_symbol],
        as_of=now,
        limit=5,
        max_stale_days=1,
    )

    assert response.state == "fresh"
    card = response.cards[0]
    assert card.commonSymbol == common_symbol
    assert card.preferredSymbol == preferred_symbol
    assert card.commonPrice == 100000
    assert card.preferredPrice == 78000
    assert card.disparityPct == 22.0
    assert card.preferredDiscountPct == 22.0
    assert card.preferredPremiumPct == -28.21
    assert card.tone == "discount"
    assert card.zScore is not None
    assert card.windows[-1].sampleCount == 3
    assert "매수·매도 신호가 아닙니다" in card.caution


@pytest.mark.asyncio
async def test_common_preferred_disparity_requires_same_source_quotes(db_session) -> None:
    now = dt.datetime(2026, 5, 14, 6, 0, tzinfo=dt.UTC)
    common_symbol = "901011"
    preferred_symbol = "901012"
    await db_session.execute(
        sa.delete(MarketQuoteSnapshot).where(
            MarketQuoteSnapshot.symbol.in_([common_symbol, preferred_symbol])
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_([common_symbol, preferred_symbol]))
    )
    await db_session.commit()
    db_session.add_all(
        [
            KRSymbolUniverse(symbol=common_symbol, name="소스테스트", exchange="KOSPI", is_active=True),
            KRSymbolUniverse(symbol=preferred_symbol, name="소스테스트우", exchange="KOSPI", is_active=True),
            MarketQuoteSnapshot(id=2500100, market="kr", symbol=common_symbol, source="kis", snapshot_at=now, price=Decimal("100000")),
            MarketQuoteSnapshot(id=2500101, market="kr", symbol=preferred_symbol, source="naver_finance", snapshot_at=now, price=Decimal("80000")),
        ]
    )
    await db_session.commit()

    response = await build_common_preferred_disparity(
        db=db_session, symbols=[common_symbol], as_of=now, limit=5
    )

    assert response.state == "missing"
    assert response.cards[0].dataState == "missing"
    assert response.cards[0].emptyReason == "same_source_quote_pair_missing"


def test_common_preferred_schema_uses_camel_case() -> None:
    response = CommonPreferredDisparityResponse(
        state="missing",
        asOf=dt.datetime(2026, 5, 14, tzinfo=dt.UTC),
        cards=[
            CommonPreferredDisparityCard(
                id="005930-005935",
                commonSymbol="005930",
                commonName="삼성전자",
                preferredSymbol="005935",
                preferredName="삼성전자우",
                dataState="missing",
                emptyReason="same_source_quote_pair_missing",
                source=DisparitySource(source="market_quote_snapshots", sourceOfTruth="market_quote_snapshots"),
            )
        ],
    )

    payload = response.model_dump(mode="json")
    assert payload["cards"][0]["commonSymbol"] == "005930"
    assert payload["cards"][0]["preferredDiscountPct"] is None
    assert "common_symbol" not in payload["cards"][0]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type("U", (), {"id": 1})()

    async def _fake_db():
        yield object()

    app.dependency_overrides[get_db] = _fake_db

    async def _stub_disparity(**kwargs):
        return CommonPreferredDisparityResponse(
            state="fresh",
            asOf=dt.datetime(2026, 5, 14, tzinfo=dt.UTC),
            cards=[
                CommonPreferredDisparityCard(
                    id="005930-005935",
                    commonSymbol="005930",
                    commonName="삼성전자",
                    preferredSymbol="005935",
                    preferredName="삼성전자우",
                    commonPrice=100000,
                    preferredPrice=78000,
                    disparityPct=22.0,
                    preferredDiscountPct=22.0,
                    preferredPremiumPct=-22.0,
                    zScore=1.25,
                    dataState="fresh",
                    tone="discount",
                    source=DisparitySource(source="kis", sourceOfTruth="market_quote_snapshots"),
                )
            ],
        )

    monkeypatch.setattr(invest_api, "build_common_preferred_disparity", _stub_disparity)
    return TestClient(app)


def test_common_preferred_disparity_router_returns_camel_case(client: TestClient) -> None:
    response = client.get("/invest/api/disparity/common-preferred?symbols=005930,005935&limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "fresh"
    assert body["cards"][0]["commonSymbol"] == "005930"
    assert body["cards"][0]["preferredSymbol"] == "005935"
    assert "common_symbol" not in body["cards"][0]
    assert "매수·매도 신호" in body["cards"][0]["caution"]


def test_common_preferred_disparity_router_enforces_limit_bounds(client: TestClient) -> None:
    response = client.get("/invest/api/disparity/common-preferred?limit=0")

    assert response.status_code == 422
