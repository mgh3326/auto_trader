from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import invest_api
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import router as invest_api_router
from app.services.invest_view_model.market_dashboard_service import (
    build_market_dashboard,
)


class _StubMarketProvider:
    async def get_indices(self) -> dict:
        return {
            "indices": [
                {
                    "symbol": "KOSPI",
                    "name": "코스피",
                    "current": 2875.25,
                    "change": 12.3,
                    "change_pct": 0.43,
                    "source": "naver",
                },
                {
                    "symbol": "KOSDAQ",
                    "name": "코스닥",
                    "current": 845.1,
                    "change": -1.2,
                    "change_pct": -0.14,
                    "source": "naver",
                },
                {
                    "symbol": "SPX",
                    "name": "S&P 500",
                    "current": 5401.0,
                    "change": 8.0,
                    "change_pct": 0.15,
                    "source": "yfinance",
                },
            ]
        }

    async def get_fear_greed(self) -> dict:
        return {"data": [{"value": "61", "value_classification": "Greed"}]}

    async def get_kimchi_premium(self) -> dict:
        return {"symbol": "BTC", "premium_pct": 2.41}


class _FailingMarketProvider(_StubMarketProvider):
    async def get_indices(self) -> dict:
        raise RuntimeError("index provider unavailable")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_dashboard_groups_sections() -> None:
    response = await build_market_dashboard(_StubMarketProvider())

    assert response.state == "fresh"
    assert [section.id for section in response.sections] == [
        "kr_market",
        "global_indices",
        "fx_macro",
        "crypto_market",
    ]
    kr_section = response.sections[0]
    assert kr_section.title == "국내 시장"
    assert [metric.symbol for metric in kr_section.metrics] == ["KOSPI", "KOSDAQ"]
    assert kr_section.metrics[0].tone == "up"
    assert kr_section.metrics[1].tone == "down"
    assert response.sections[3].metrics[0].value == "2.41"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_dashboard_degrades_to_partial_on_provider_error() -> None:
    response = await build_market_dashboard(_FailingMarketProvider())

    assert response.state == "partial"
    assert any("market_index" in warning for warning in response.warnings)
    assert response.sections[0].state == "missing"
    assert response.sections[1].state == "missing"
    assert response.sections[2].state == "fresh"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_dashboard_captures_providers_concurrently() -> None:
    class _ConcurrentProvider(_StubMarketProvider):
        def __init__(self) -> None:
            self.active = 0
            self.peak_active = 0

        async def _enter(self) -> None:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1

        async def get_indices(self) -> dict:
            await self._enter()
            return await super().get_indices()

        async def get_fear_greed(self) -> dict:
            await self._enter()
            return await super().get_fear_greed()

        async def get_kimchi_premium(self) -> dict:
            await self._enter()
            return await super().get_kimchi_premium()

    provider = _ConcurrentProvider()

    response = await build_market_dashboard(provider)

    assert response.state == "fresh"
    assert provider.peak_active == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_dashboard_emits_provider_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.invest_view_model import market_dashboard_service as service

    started: list[tuple[str, str]] = []

    class _Span:
        def set_data(self, key: str, value: object) -> None:
            return None

        def set_tag(self, key: str, value: object) -> None:
            return None

    class _SpanContext:
        def __init__(self, op: str, name: str) -> None:
            self.op = op
            self.name = name

        def __enter__(self) -> _Span:
            started.append((self.op, self.name))
            return _Span()

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: object) -> _SpanContext:
        return _SpanContext(op, name)

    monkeypatch.setattr(service.sentry_sdk, "start_span", _start_span)

    await build_market_dashboard(_StubMarketProvider())

    assert ("invest.market.provider", "invest.market.market_index") in started
    assert ("invest.market.provider", "invest.market.fear_greed") in started
    assert ("invest.market.provider", "invest.market.kimchi_premium") in started


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()

    async def _stub_dashboard():
        return await build_market_dashboard(_StubMarketProvider())

    monkeypatch.setattr(invest_api, "build_market_dashboard", _stub_dashboard)
    return TestClient(app)


@pytest.mark.unit
def test_get_market_dashboard_returns_read_only_payload(client: TestClient) -> None:
    response = client.get("/invest/api/market")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "fresh"
    assert [section["id"] for section in body["sections"]] == [
        "kr_market",
        "global_indices",
        "fx_macro",
        "crypto_market",
    ]
    assert body["sections"][0]["metrics"][0]["symbol"] == "KOSPI"
    notes = " ".join(body["notes"]).lower()
    assert "mutations" in notes
    assert all(
        "order_id" not in metric
        for section in body["sections"]
        for metric in section["metrics"]
    )
