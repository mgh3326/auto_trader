from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.invest_action_readiness import KrActionReadinessResponse
from app.schemas.invest_coverage import InvestCoverageResponse, InvestCoverageSurface
from app.schemas.invest_home import (
    Account,
    Holding,
    HomeSummary,
    InvestHomeResponse,
)
from app.services.invest_view_model import action_readiness_service as service_module


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _OneResult:
    def __init__(self, value: tuple[Any, ...]) -> None:
        self.value = value

    def one(self) -> tuple[Any, ...]:
        return self.value


class _FakeDb:
    def __init__(self, results: Iterable[Any]) -> None:
        self.results = list(results)
        self.execute_count = 0

    async def execute(self, _stmt: Any, _params: Any = None) -> Any:
        self.execute_count += 1
        if not self.results:
            raise AssertionError("unexpected execute call")
        return self.results.pop(0)


class _FakeHomeService:
    def __init__(self, home: InvestHomeResponse) -> None:
        self.home = home
        self.user_ids: list[int] = []

    async def get_home(self, *, user_id: int, **_kwargs: Any) -> InvestHomeResponse:
        self.user_ids.append(user_id)
        return self.home


def _surface(name: str, state: str = "fresh") -> InvestCoverageSurface:
    return InvestCoverageSurface(
        surface=name,
        label=name,
        state=state,  # type: ignore[arg-type]
        market="kr",
        sourceOfTruth=f"{name}_read_model",
        references=["toss"],
    )


def _coverage() -> InvestCoverageResponse:
    now = dt.datetime(2026, 5, 14, 9, 0, tzinfo=dt.UTC)
    return InvestCoverageResponse(
        market="kr",
        asOf=now,
        tradingDate=now.date(),
        states=["fresh", "missing", "provider_unwired"],
        surfaces=[
            _surface("quotes"),
            _surface("ohlcv"),
            _surface("orderbook_nxt_capability"),
            _surface("screener_snapshots"),
            _surface("investor_flow"),
            _surface("news_feed"),
            _surface("calendar_events"),
            _surface("valuation_fundamentals"),
            _surface("research_reports"),
            _surface("pending_orders"),
        ],
        symbols=[],
        gaps=[],
        notes=[],
    )


def _home(
    *, symbol: str = "005930", sellable_quantity: float | None = 1.0
) -> InvestHomeResponse:
    return InvestHomeResponse(
        homeSummary=HomeSummary(
            includedSources=["kis"],
            excludedSources=[],
            totalValueKrw=100_000.0,
        ),
        accounts=[
            Account(
                accountId="kis-live",
                displayName="KIS live",
                source="kis",
                accountKind="live",
                includedInHome=True,
                valueKrw=100_000.0,
                buyingPower={"krw": 50_000.0},
                cashBalances={"krw": 50_000.0},
            )
        ],
        holdings=[
            Holding(
                holdingId="kis-live-005930",
                accountId="kis-live",
                source="kis",
                accountKind="live",
                symbol=symbol,
                market="KR",
                assetType="equity",
                assetCategory="kr_stock",
                displayName="삼성전자",
                quantity=2.0,
                currency="KRW",
                sellableQuantity=sellable_quantity,
            )
        ],
        groupedHoldings=[],
    )


def test_kr_action_readiness_schema_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        KrActionReadinessResponse(
            asOf=dt.datetime.now(dt.UTC),
            overallState="ready",
            canGenerateBuyReport=True,
            canGenerateSellReport=True,
            families=[],
            sourcePolicy=[],
            unexpected="nope",
        )


@pytest.mark.asyncio
async def test_build_kr_action_readiness_uses_empty_symbol_list_and_kis_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_coverage(db: Any, **kwargs: Any) -> InvestCoverageResponse:
        calls.append(kwargs)
        return _coverage()

    monkeypatch.setattr(service_module, "build_invest_coverage", fake_coverage)

    response = await service_module.build_kr_action_readiness(
        db=_FakeDb([_OneResult((1, dt.datetime(2026, 5, 14, tzinfo=dt.UTC)))]),
        user_id=7,
        home_service=_FakeHomeService(_home()),
        symbol=None,
    )

    assert calls == [{"market": "kr", "symbols": []}]
    family_by_key = {family.key: family for family in response.families}
    assert family_by_key["kis_live_cash_orderable"].authority == "kis_live_broker"
    assert family_by_key["kis_live_holdings"].state == "ready"
    assert family_by_key["kis_live_open_orders"].state == "blocked"
    assert family_by_key["pending_order_reconciliation"].state == "blocked"
    assert family_by_key["execution_ledger"].state == "unknown"
    assert family_by_key["sell_history"].state == "unknown"
    assert family_by_key["research_consensus"].state == "unknown"
    assert family_by_key["naver_momentum_events"].authority == "auto_trader_read_model"
    assert (
        family_by_key["naver_momentum_events"].sourceOfTruth
        == "auto_trader_read_model/unwired"
    )
    assert "naver_reference" in family_by_key["naver_momentum_events"].references
    assert response.canGenerateBuyReport is False
    assert any("KIS live 미체결 주문" in blocker for blocker in response.blockers)
    assert response.notes[0].startswith("Read-only readiness only")


@pytest.mark.asyncio
async def test_symbol_scoped_sell_readiness_blocks_when_live_holding_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_coverage(db: Any, **kwargs: Any) -> InvestCoverageResponse:
        calls.append(kwargs)
        return _coverage()

    monkeypatch.setattr(service_module, "build_invest_coverage", fake_coverage)

    response = await service_module.build_kr_action_readiness(
        db=_FakeDb(
            [
                _ScalarResult("000660"),
                _OneResult((0, None)),
                _ScalarResult(dt.datetime(2026, 5, 14, 9, 0, tzinfo=dt.UTC)),
                _OneResult(
                    (
                        dt.datetime(2026, 5, 14, 9, 0, tzinfo=dt.UTC),
                        dt.date(2026, 5, 14),
                    )
                ),
                _ScalarResult(dt.date(2026, 5, 14)),
            ]
        ),
        user_id=7,
        home_service=_FakeHomeService(_home(symbol="005930")),
        symbol="000660",
    )

    assert calls == [{"market": "kr", "symbols": ["000660"]}]
    family_by_key = {family.key: family for family in response.families}
    assert family_by_key["kis_live_sellable_quantity"].state == "blocked"
    assert response.canGenerateSellReport is False
    assert any("000660" in blocker for blocker in response.blockers)


@pytest.mark.asyncio
async def test_invalid_kr_symbol_returns_blocked_without_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_coverage(db: Any, **kwargs: Any) -> InvestCoverageResponse:
        raise AssertionError("coverage should not run for invalid symbols")

    monkeypatch.setattr(service_module, "build_invest_coverage", fail_coverage)

    response = await service_module.build_kr_action_readiness(
        db=_FakeDb([]),
        user_id=7,
        home_service=_FakeHomeService(_home()),
        symbol="AAPL",
    )

    assert response.overallState == "blocked"
    assert response.canGenerateBuyReport is False
    assert response.canGenerateSellReport is False
    assert response.families == []
    assert response.blockers == [
        "symbol_not_kr_equity: KR 심볼은 6자리 종목코드여야 합니다. 확인 불가"
    ]
