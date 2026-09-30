from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.services.brokers.toss.auth import TossOAuthTokenManager
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.errors import (
    TossApiResponseError,
    TossRateLimitError,
    TossResponseContractError,
)
from app.services.brokers.toss.rate_limiter import TossApiGroup, TossRateLimiter

TOSS_DIR = Path("app/services/brokers/toss")


class _TokenManager(TossOAuthTokenManager):
    def __init__(self) -> None:
        pass

    async def get_access_token(
        self, *, force_reissue: bool = False, failed_token: str | None = None
    ) -> str:
        del force_reissue, failed_token
        return "token-1"


class _RecordingLimiter(TossRateLimiter):
    """Captures the rate group every request is metered under."""

    def __init__(self) -> None:
        super().__init__()
        self.acquired: list[TossApiGroup] = []

    async def acquire(self, group: TossApiGroup) -> None:
        self.acquired.append(group)


def _json(payload):
    return {"result": payload}


def _volume(buy: str, sell: str, net: str) -> dict:
    return {"buyVolume": buy, "sellVolume": sell, "netBuyVolume": net}


def _amount(buy: str, sell: str) -> dict:
    return {"buyAmount": buy, "sellAmount": sell}


_CONFIRMED_RECORD = {
    "date": "2026-07-16",
    "updatedAt": "2026-07-16T17:29:12+09:00",
    "individual": _volume("8412300", "8120450", "291850"),
    "foreigner": _volume("5120000", "4980000", "140000"),
    "institution": {
        "buyVolume": "1953200",
        "sellVolume": "1915300",
        "netBuyVolume": "37900",
        "breakdown": {
            "financialInvestment": _volume("900000", "850000", "50000"),
            "insurance": _volume("100000", "90000", "10000"),
            "trust": _volume("200000", "210000", "-10000"),
            "privateEquityFund": _volume("150000", "140000", "10000"),
            "bank": _volume("500000", "520000", "-20000"),
            "otherFinancialInstitution": _volume("3200", "5300", "-2100"),
            "pensionFund": _volume("100000", "100000", "0"),
        },
    },
    "otherCorporation": _volume("300000", "310000", "-10000"),
    "foreignerHolding": {
        "holdingQuantity": "3012456789",
        "limitQuantity": "5919637922",
        "holdingRate": "0.5089",
    },
    "cfd": {
        "buyBalanceQuantity": "1250000",
        "buyBalanceRate": "0.0002",
        "sellBalanceQuantity": "890000",
        "sellBalanceRate": "0.0001",
    },
    "undocumentedExtraField": {"tolerated": True},
}

_PROVISIONAL_RECORD = {
    "date": "2026-07-16",
    "updatedAt": "2026-07-16T12:00:00+09:00",
    "individual": None,
    "foreigner": _volume("1200000", "1100000", "100000"),
    "institution": {
        "buyVolume": "900000",
        "sellVolume": "800000",
        "netBuyVolume": "100000",
        "breakdown": None,
    },
    "otherCorporation": None,
    "foreignerHolding": None,
    "cfd": None,
}


def _client(handler, limiter: _RecordingLimiter | None = None) -> TossReadClient:
    return TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(handler),
        rate_limiter=limiter or _RecordingLimiter(),
        publish_error_signals=False,
    )


@pytest.mark.asyncio
async def test_stock_investor_trading_path_params_group_and_parse() -> None:
    limiter = _RecordingLimiter()
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=_json({"records": [_CONFIRMED_RECORD], "nextUntil": None}),
            request=request,
        )

    client = _client(handler, limiter)
    try:
        page = await client.stock_investor_trading(
            "005930", count=50, until="2026-07-31"
        )
    finally:
        await client.aclose()

    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/stocks/005930/investor-trading"
    assert seen["params"] == {"count": "50", "until": "2026-07-31"}
    assert limiter.acquired == [TossApiGroup.STOCK_TRADING_TREND]

    record = page.records[0]
    assert page.next_until is None
    assert record.date == "2026-07-16"
    assert record.updated_at == "2026-07-16T17:29:12+09:00"
    assert record.individual is not None
    assert record.individual.net_buy_volume == Decimal("291850")
    assert record.foreigner.buy_volume == Decimal("5120000")
    assert record.institution.net_buy_volume == Decimal("37900")
    assert record.institution.breakdown is not None
    assert record.institution.breakdown.financial_investment.buy_volume == Decimal(
        "900000"
    )
    assert record.other_corporation is not None
    assert record.foreigner_holding is not None
    assert record.foreigner_holding.holding_rate == Decimal("0.5089")
    assert record.cfd is not None
    assert record.cfd.sell_balance_rate == Decimal("0.0001")


@pytest.mark.asyncio
async def test_stock_investor_trading_provisional_nullable_sections() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_json({"records": [_PROVISIONAL_RECORD], "nextUntil": None}),
            request=request,
        )

    client = _client(handler)
    try:
        page = await client.stock_investor_trading("005930")
    finally:
        await client.aclose()

    record = page.records[0]
    assert record.individual is None
    assert record.other_corporation is None
    assert record.foreigner_holding is None
    assert record.cfd is None
    assert record.institution.breakdown is None
    assert record.foreigner.net_buy_volume == Decimal("100000")


@pytest.mark.asyncio
async def test_stock_investor_trading_pagination_terminates_on_null() -> None:
    limiter = _RecordingLimiter()
    untils: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        until = request.url.params.get("until")
        untils.append(until)
        if until is None:
            return httpx.Response(
                200,
                json=_json({"records": [_CONFIRMED_RECORD], "nextUntil": "2026-07-10"}),
                request=request,
            )
        return httpx.Response(
            200,
            json=_json({"records": [_PROVISIONAL_RECORD], "nextUntil": None}),
            request=request,
        )

    client = _client(handler, limiter)
    try:
        records = await client.collect_stock_investor_trading("005930")
    finally:
        await client.aclose()

    assert untils == [None, "2026-07-10"]
    assert len(records) == 2
    assert records[0].date == "2026-07-16" and records[1].date == "2026-07-16"
    assert limiter.acquired == [
        TossApiGroup.STOCK_TRADING_TREND,
        TossApiGroup.STOCK_TRADING_TREND,
    ]


@pytest.mark.asyncio
async def test_stock_investor_trading_empty_page_terminates() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json=_json({"records": [], "nextUntil": None}), request=request
        )

    client = _client(handler)
    try:
        records = await client.collect_stock_investor_trading("005930")
    finally:
        await client.aclose()

    assert calls == 1
    assert records == []


@pytest.mark.asyncio
async def test_stock_investor_trading_max_pages_bounds_walk() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_json({"records": [_CONFIRMED_RECORD], "nextUntil": "2026-01-01"}),
            request=request,
        )

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="max_pages"):
            await client.collect_stock_investor_trading("005930", max_pages=3)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stock_investor_trading_count_bound() -> None:
    client = _client(lambda request: httpx.Response(500, request=request))
    try:
        with pytest.raises(ValueError, match="1..100"):
            await client.stock_investor_trading("005930", count=101)
        with pytest.raises(ValueError, match="1..100"):
            await client.stock_investor_trading("005930", count=0)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_market_indicator_prices_path_params_group_and_parse() -> None:
    limiter = _RecordingLimiter()
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["symbols"] = request.url.params["symbols"]
        return httpx.Response(
            200,
            json=_json(
                [
                    {
                        "symbol": "KOSPI",
                        "timestamp": "2026-06-11T15:30:00+09:00",
                        "lastPrice": "2812.45",
                    },
                    {
                        "symbol": "KOSDAQ",
                        "timestamp": None,
                        "lastPrice": "798.10",
                        "surpriseField": 1,
                    },
                ]
            ),
            request=request,
        )

    client = _client(handler, limiter)
    try:
        prices = await client.market_indicator_prices(["KOSPI", "KOSDAQ"])
    finally:
        await client.aclose()

    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/market-indicators/prices"
    assert seen["symbols"] == "KOSPI,KOSDAQ"
    assert limiter.acquired == [TossApiGroup.MARKET_INDICATOR]
    assert prices[0].last_price == Decimal("2812.45")
    assert prices[1].symbol == "KOSDAQ" and prices[1].timestamp is None


@pytest.mark.asyncio
async def test_market_indicator_investor_trading_params_group_parse() -> None:
    limiter = _RecordingLimiter()
    seen = {}

    record = {
        "date": "2026-06-11",
        "updatedAt": "2026-06-11T18:10:00+09:00",
        "individual": _amount("5200000000000", "5350000000000"),
        "foreigner": _amount("1200000000000", "900000000000"),
        "institution": {
            "buyAmount": "2100000000000",
            "sellAmount": "2180000000000",
            "breakdown": {
                "financialInvestment": _amount("800000000000", "700000000000"),
                "insurance": _amount("200000000000", "210000000000"),
                "trust": _amount("300000000000", "290000000000"),
                "privateEquityFund": _amount("100000000000", "110000000000"),
                "bank": _amount("150000000000", "160000000000"),
                "otherFinancialInstitution": _amount("150000000000", "180000000000"),
                "pensionFund": _amount("400000000000", "530000000000"),
            },
        },
        "otherCorporation": _amount("300000000000", "290000000000"),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=_json({"records": [record], "nextUntil": None}),
            request=request,
        )

    client = _client(handler, limiter)
    try:
        page = await client.market_indicator_investor_trading(
            "KOSPI", interval="1d", count=20
        )
    finally:
        await client.aclose()

    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/market-indicators/KOSPI/investor-trading"
    assert seen["params"] == {"interval": "1d", "count": "20"}
    assert limiter.acquired == [TossApiGroup.MARKET_INDICATOR]

    row = page.records[0]
    assert row.individual.buy_amount == Decimal("5200000000000")
    assert row.institution.sell_amount == Decimal("2180000000000")
    assert row.institution.breakdown.pension_fund.sell_amount == Decimal("530000000000")
    assert row.other_corporation.buy_amount == Decimal("300000000000")


@pytest.mark.asyncio
async def test_market_indicator_investor_trading_rejects_bad_inputs() -> None:
    client = _client(lambda request: httpx.Response(500, request=request))
    try:
        with pytest.raises(ValueError, match="KOSPI/KOSDAQ"):
            await client.market_indicator_investor_trading("005930", interval="1d")
        with pytest.raises(ValueError, match="1d/1w/1mo/1y"):
            await client.market_indicator_investor_trading("KOSPI", interval="1m")
        with pytest.raises(ValueError, match="1..100"):
            await client.market_indicator_investor_trading(
                "KOSPI", interval="1d", count=101
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_market_indicator_investor_trading_pagination() -> None:
    untils: list[str | None] = []
    record = {
        "date": "2026-06-11",
        "updatedAt": "2026-06-11T18:10:00+09:00",
        "individual": _amount("1", "2"),
        "foreigner": _amount("3", "4"),
        "institution": {
            "buyAmount": "5",
            "sellAmount": "6",
            "breakdown": {
                "financialInvestment": _amount("1", "1"),
                "insurance": _amount("1", "1"),
                "trust": _amount("1", "1"),
                "privateEquityFund": _amount("1", "1"),
                "bank": _amount("1", "1"),
                "otherFinancialInstitution": _amount("0", "0"),
                "pensionFund": _amount("0", "1"),
            },
        },
        "otherCorporation": _amount("7", "8"),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        until = request.url.params.get("until")
        untils.append(until)
        nxt = "2026-06-01" if until is None else None
        return httpx.Response(
            200,
            json=_json({"records": [record], "nextUntil": nxt}),
            request=request,
        )

    client = _client(handler)
    try:
        records = await client.collect_market_indicator_investor_trading(
            "KOSDAQ", interval="1w"
        )
    finally:
        await client.aclose()

    assert untils == [None, "2026-06-01"]
    assert len(records) == 2


@pytest.mark.asyncio
async def test_rankings_us_market_params_group_and_parse() -> None:
    limiter = _RecordingLimiter()
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=_json(
                {
                    "rankedAt": "2026-06-10T14:30:00+09:00",
                    "rankings": [
                        {
                            "rank": 1,
                            "symbol": "AAPL",
                            "currency": "USD",
                            "price": {
                                "lastPrice": "185.70",
                                "basePrice": "183.41",
                                "changeRate": "0.0125",
                            },
                            "tradingVolume": "18432100",
                            "tradingAmount": "3422200000",
                            "extraRankingField": "x",
                        },
                        {
                            "rank": 2,
                            "symbol": "TSLA",
                            "currency": "USD",
                            "price": {
                                "lastPrice": "210.00",
                                "basePrice": "0",
                                "changeRate": None,
                            },
                            "tradingVolume": "9000000",
                            "tradingAmount": "1890000000",
                        },
                    ],
                }
            ),
            request=request,
        )

    client = _client(handler, limiter)
    try:
        result = await client.rankings(
            ranking_type="MARKET_TRADING_AMOUNT",
            market_country="US",
            duration="realtime",
            count=25,
            exclude_investment_caution=True,
        )
    finally:
        await client.aclose()

    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/rankings"
    assert seen["params"] == {
        "type": "MARKET_TRADING_AMOUNT",
        "marketCountry": "US",
        "duration": "realtime",
        "excludeInvestmentCaution": "true",
        "count": "25",
    }
    assert limiter.acquired == [TossApiGroup.RANKING]

    assert result.ranked_at == "2026-06-10T14:30:00+09:00"
    first, second = result.rankings
    assert first.rank == 1 and first.symbol == "AAPL" and first.currency == "USD"
    assert first.price.change_rate == Decimal("0.0125")
    assert first.trading_amount == Decimal("3422200000")
    assert second.price.change_rate is None


@pytest.mark.asyncio
async def test_rankings_top_gainers_kr_and_empty_ranked_at() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_json({"rankedAt": None, "rankings": []}), request=request
        )

    client = _client(handler)
    try:
        result = await client.rankings(
            ranking_type="TOP_GAINERS", market_country="KR", duration="1d"
        )
    finally:
        await client.aclose()

    assert result.rankings == [] and result.ranked_at is None


@pytest.mark.asyncio
async def test_rankings_rejects_bad_inputs() -> None:
    client = _client(lambda request: httpx.Response(500, request=request))
    try:
        with pytest.raises(ValueError, match="count"):
            await client.rankings(
                ranking_type="MARKET_TRADING_AMOUNT",
                market_country="KR",
                duration="realtime",
                count=101,
            )
        with pytest.raises(ValueError, match="realtime"):
            await client.rankings(
                ranking_type="TOP_LOSERS",
                market_country="KR",
                duration="realtime",
            )
        with pytest.raises(ValueError, match="ranking type"):
            await client.rankings(
                ranking_type="NONSENSE", market_country="KR", duration="1d"
            )
        with pytest.raises(ValueError, match="market"):
            await client.rankings(
                ranking_type="TOP_GAINERS", market_country="JP", duration="1d"
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stocks_all_delisted_params_group_and_parse() -> None:
    limiter = _RecordingLimiter()
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=_json(
                [
                    {
                        "symbol": "000020",
                        "name": "동화약품",
                        "securityType": "STOCK",
                        "isCommonShare": True,
                        "isinCode": "KR7000020008",
                    },
                    {
                        "symbol": "001040",
                        "name": "CJ4우(전환)",
                        "securityType": "STOCK",
                        "isCommonShare": False,
                        "isinCode": "KR7001041006",
                        "unlistedField": "ignored",
                    },
                ]
            ),
            request=request,
        )

    client = _client(handler, limiter)
    try:
        stocks = await client.stocks_all(
            market="KOSPI", status="DELISTED", security_type="STOCK"
        )
    finally:
        await client.aclose()

    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/stocks/all"
    assert seen["params"] == {
        "market": "KOSPI",
        "status": "DELISTED",
        "securityType": "STOCK",
    }
    assert limiter.acquired == [TossApiGroup.STOCK_ALL]
    assert [s.symbol for s in stocks] == ["000020", "001040"]
    assert stocks[1].is_common_share is False


@pytest.mark.asyncio
async def test_stocks_all_common_share_param_and_rejects() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_json([]), request=request)

    client = _client(handler)
    try:
        await client.stocks_all(market="NASDAQ", common_share=True)
        assert seen["params"] == {"market": "NASDAQ", "commonShare": "true"}
        with pytest.raises(ValueError, match="market"):
            await client.stocks_all(market="BIST")
        with pytest.raises(ValueError, match="status"):
            await client.stocks_all(market="KOSPI", status="BROKEN")
        with pytest.raises(ValueError, match="securityType"):
            await client.stocks_all(market="KOSPI", security_type="BOND")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_new_methods_map_429_to_typed_rate_limit_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": {
                    "requestId": "req-429",
                    "code": "rate-limit-exceeded",
                    "message": "slow down",
                }
            },
            headers={"Retry-After": "1"},
            request=request,
        )

    client = TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(handler),
        rate_limiter=_RecordingLimiter(),
        retry_on_429=False,
        publish_error_signals=False,
    )
    try:
        with pytest.raises(TossRateLimitError) as exc_info:
            await client.rankings(
                ranking_type="TOP_GAINERS", market_country="KR", duration="1d"
            )
    finally:
        await client.aclose()

    assert exc_info.value.status_code == 429
    assert exc_info.value.envelope.code == "rate-limit-exceeded"


@pytest.mark.asyncio
async def test_new_methods_map_error_envelope_to_typed_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "requestId": "req-400",
                    "code": "unsupported-symbol",
                    "message": "not in catalog",
                }
            },
            request=request,
        )

    client = _client(handler)
    try:
        with pytest.raises(TossApiResponseError) as exc_info:
            await client.market_indicator_prices(["KR_BOND_99Y"])
    finally:
        await client.aclose()

    assert exc_info.value.status_code == 400
    assert exc_info.value.envelope.code == "unsupported-symbol"


@pytest.mark.asyncio
async def test_missing_required_fields_raise_typed_contract_error() -> None:
    bad_record = dict(_CONFIRMED_RECORD)
    del bad_record["date"]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_json({"records": [bad_record], "nextUntil": None}),
            request=request,
        )

    client = _client(handler)
    try:
        with pytest.raises(TossResponseContractError):
            await client.stock_investor_trading("005930")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_missing_rankings_field_raises_typed_contract_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_json({"rankedAt": None}), request=request)

    client = _client(handler)
    try:
        with pytest.raises(TossResponseContractError):
            await client.rankings(
                ranking_type="TOP_GAINERS", market_country="KR", duration="1d"
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_missing_listed_stock_field_raises_typed_contract_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_json(
                [
                    {
                        "symbol": "000020",
                        "name": "동화약품",
                        "securityType": "STOCK",
                        "isCommonShare": True,
                    }
                ]
            ),
            request=request,
        )

    client = _client(handler)
    try:
        with pytest.raises(TossResponseContractError):
            await client.stocks_all(market="KOSPI")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_all_new_methods_only_send_get_requests() -> None:
    methods_seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods_seen.append(request.method)
        path = request.url.path
        if path.endswith("/investor-trading"):
            payload = {"records": [], "nextUntil": None}
        elif path == "/api/v1/rankings":
            payload = {"rankedAt": None, "rankings": []}
        else:
            payload = []
        return httpx.Response(200, json=_json(payload), request=request)

    client = _client(handler)
    try:
        await client.stock_investor_trading("005930")
        await client.market_indicator_prices(["KOSPI"])
        await client.market_indicator_investor_trading("KOSPI", interval="1d")
        await client.rankings(
            ranking_type="TOP_GAINERS", market_country="KR", duration="1d"
        )
        await client.stocks_all(market="KOSPI")
    finally:
        await client.aclose()

    assert methods_seen == ["GET"] * 5


def test_new_methods_route_through_shared_limiter_and_get_only() -> None:
    """Static guard: the #1064 methods must not construct a private limiter,
    must meter every call under an explicit TossApiGroup, and must stay
    read-only (no order-group / POST paths)."""
    source = (TOSS_DIR / "client.py").read_text()
    new_methods = [
        "stock_investor_trading",
        "collect_stock_investor_trading",
        "market_indicator_prices",
        "market_indicator_investor_trading",
        "collect_market_indicator_investor_trading",
        "rankings",
        "stocks_all",
    ]
    for name in new_methods:
        body = source.split(f"async def {name}", 1)[1].split("async def", 1)[0]
        # No private limiter in any new method; the ctor's documented
        # ``rate_limiter or TossRateLimiter()`` default is outside these bodies.
        assert "TossRateLimiter(" not in body
        if name.startswith("collect_"):
            # Pagination helpers must delegate to the page methods, not
            # bypass them with a direct _request call.
            assert "self._request(" not in body
            continue
        assert '"GET"' in body
        assert "group=TossApiGroup." in body
        assert "TossApiGroup.ORDER" not in body
        assert "account_required" not in body
