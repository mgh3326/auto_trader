"""#1086 strict Toss 1m candles: request shape, rate group, pagination, contract.

Recorded/fake responses only (httpx.MockTransport); no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.services.brokers.toss.auth import TossOAuthTokenManager
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import parse_minute_candle_page
from app.services.brokers.toss.errors import (
    TossApiResponseError,
    TossPaginationCapExceeded,
    TossResponseContractError,
)
from app.services.brokers.toss.rate_limiter import TossApiGroup, TossRateLimiter

KST = timezone(timedelta(hours=9))


class _TokenManager(TossOAuthTokenManager):
    def __init__(self) -> None:
        pass

    async def get_access_token(
        self, *, force_reissue: bool = False, failed_token: str | None = None
    ) -> str:
        del force_reissue, failed_token
        return "token-1"


class _RecordingLimiter(TossRateLimiter):
    def __init__(self) -> None:
        super().__init__()
        self.acquired: list[TossApiGroup] = []

    async def acquire(self, group: TossApiGroup) -> None:
        self.acquired.append(group)


def _bar(ts: str, price: str = "72000", volume: str = "100") -> dict:
    return {
        "timestamp": ts,
        "openPrice": price,
        "highPrice": price,
        "lowPrice": price,
        "closePrice": price,
        "volume": volume,
        "currency": "KRW",
    }


def _page(bars: list[dict], next_before: str | None) -> dict:
    return {"result": {"candles": bars, "nextBefore": next_before}}


def _client(handler, limiter=None) -> TossReadClient:
    return TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(handler),
        rate_limiter=limiter or _RecordingLimiter(),
        publish_error_signals=False,
    )


def _query(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


# --- the openapi.json v1.2.19 minuteCandles example, verbatim ---------------
SPEC_MINUTE_EXAMPLE = {
    "candles": [
        {
            "timestamp": "2026-03-25T09:32:00+09:00",
            "openPrice": "72000",
            "highPrice": "72100",
            "lowPrice": "71950",
            "closePrice": "72050",
            "volume": "15200",
            "currency": "KRW",
        },
        {
            "timestamp": "2026-03-25T09:31:00+09:00",
            "openPrice": "71950",
            "highPrice": "72050",
            "lowPrice": "71900",
            "closePrice": "72000",
            "volume": "18400",
            "currency": "KRW",
        },
    ],
    "nextBefore": "2026-03-25T09:31:00+09:00",
}


def test_spec_example_parses_with_end_labelled_timestamp():
    page = parse_minute_candle_page(SPEC_MINUTE_EXAMPLE)
    first = page.candles[0]
    assert first.timestamp == datetime(2026, 3, 25, 9, 32, tzinfo=KST)
    assert first.bar_start == datetime(2026, 3, 25, 9, 31, tzinfo=KST)
    assert first.close_price == Decimal("72050")
    assert page.next_before_raw == "2026-03-25T09:31:00+09:00"
    assert page.next_before == datetime(2026, 3, 25, 9, 31, tzinfo=KST)


def test_unknown_fields_are_tolerated():
    raw = {
        "candles": [{**_bar("2026-03-25T09:32:00+09:00"), "extra": 1}],
        "nextBefore": None,
        "undocumented": True,
    }
    assert len(parse_minute_candle_page(raw).candles) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("closePrice"),
        lambda b: b.pop("currency"),
        lambda b: b.update(volume=100),  # bare number, not a decimal string
        lambda b: b.update(openPrice="NaN"),
        lambda b: b.update(timestamp="2026-03-25T09:32:00"),  # no offset
        lambda b: b.update(timestamp="2026-03-25T09:32:30+09:00"),  # not aligned
        lambda b: b.update(timestamp=1711326720),
        lambda b: b.update(lowPrice="73000"),  # low above open/close
        lambda b: b.update(volume="-1"),
    ],
)
def test_bar_contract_errors(mutate):
    bar = _bar("2026-03-25T09:32:00+09:00")
    mutate(bar)
    with pytest.raises(TossResponseContractError):
        parse_minute_candle_page({"candles": [bar], "nextBefore": None})


@pytest.mark.parametrize(
    "raw",
    [
        {"nextBefore": None},
        {"candles": {}, "nextBefore": None},
        {"candles": [], "nextBefore": 5},
        {"candles": [], "nextBefore": "not-a-date"},
        [],
        None,
    ],
)
def test_page_contract_errors(raw):
    with pytest.raises(TossResponseContractError):
        parse_minute_candle_page(raw)


def test_page_must_be_newest_first():
    raw = {
        "candles": [
            _bar("2026-03-25T09:31:00+09:00"),
            _bar("2026-03-25T09:32:00+09:00"),
        ],
        "nextBefore": None,
    }
    with pytest.raises(TossResponseContractError):
        parse_minute_candle_page(raw)


@pytest.mark.asyncio
async def test_request_shape_and_rate_group():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_page([], None))

    limiter = _RecordingLimiter()
    client = _client(handler, limiter)
    await client.minute_candles(
        "005930", adjusted=False, before="2026-03-25T09:00:00+09:00"
    )
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/api/v1/candles"
    assert "X-Tossinvest-Account" not in request.headers
    # The spec requires the offset '+' to be sent as %2B.
    assert "before=2026-03-25T09%3A00%3A00%2B09%3A00" in str(request.url)
    assert _query(request) == {
        "symbol": "005930",
        "interval": "1m",
        "count": "200",
        "adjusted": "false",
        "before": "2026-03-25T09:00:00+09:00",
    }
    assert limiter.acquired == [TossApiGroup.MARKET_DATA_CHART]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"count": 0},
        {"count": 201},
        {"count": True},
        {"before": "2026-03-25T09:00:00"},  # naive
        {"before": "yesterday"},
    ],
)
async def test_request_validation(kwargs):
    client = _client(lambda r: httpx.Response(200, json=_page([], None)))
    with pytest.raises(ValueError):
        await client.minute_candles("005930", adjusted=False, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["..", "005930/../x", "005930\n"])
async def test_symbol_validation(symbol):
    client = _client(lambda r: httpx.Response(200, json=_page([], None)))
    with pytest.raises(ValueError):
        await client.minute_candles(symbol, adjusted=False)


@pytest.mark.asyncio
async def test_api_error_surfaces_typed():
    def handler(request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "requestId": "r",
                    "code": "invalid-request",
                    "message": "지원하지 않는 캔들 주기입니다.",
                    "data": {},
                }
            },
        )

    with pytest.raises(TossApiResponseError) as exc_info:
        await _client(handler).minute_candles("005930", adjusted=False)
    assert exc_info.value.envelope.code == "invalid-request"


# --- pagination --------------------------------------------------------------


def _paged_handler(pages: dict[str | None, dict]):
    calls: list[str | None] = []

    def handler(request):
        before = _query(request).get("before")
        calls.append(before)
        return httpx.Response(200, json=pages[before])

    return handler, calls


@pytest.mark.asyncio
async def test_pagination_follows_next_before_until_null_and_dedupes_boundary():
    top = "2026-03-26T20:00:00+09:00"
    pages = {
        top: _page(
            [_bar("2026-03-26T09:02:00+09:00"), _bar("2026-03-26T09:01:00+09:00")],
            "2026-03-26T09:01:00+09:00",
        ),
        # inclusive before: the boundary bar comes back once more.
        "2026-03-26T09:01:00+09:00": _page(
            [_bar("2026-03-26T09:01:00+09:00"), _bar("2026-03-25T18:11:00+09:00")],
            None,
        ),
    }
    handler, calls = _paged_handler(pages)
    limiter = _RecordingLimiter()
    paced: list[int] = []

    async def pace():
        paced.append(1)

    bars = await _client(handler, limiter).collect_minute_candles(
        "005930",
        adjusted=False,
        before=top,
        not_before=datetime(2026, 3, 25, tzinfo=KST),
        pace=pace,
    )
    assert calls == [top, "2026-03-26T09:01:00+09:00"]
    assert [b.timestamp.strftime("%d %H:%M") for b in bars] == [
        "25 18:11",
        "26 09:01",
        "26 09:02",
    ]
    assert limiter.acquired == [TossApiGroup.MARKET_DATA_CHART] * 2
    assert len(paced) == 2


@pytest.mark.asyncio
async def test_pagination_stops_on_empty_page():
    top = "2026-03-26T20:00:00+09:00"
    handler, calls = _paged_handler({top: _page([], "2026-03-26T19:00:00+09:00")})
    bars = await _client(handler).collect_minute_candles(
        "005930",
        adjusted=False,
        before=top,
        not_before=datetime(2026, 3, 25, tzinfo=KST),
    )
    assert bars == [] and calls == [top]


@pytest.mark.asyncio
async def test_pagination_stops_once_past_not_before_and_filters_older():
    top = "2026-03-26T20:00:00+09:00"
    pages = {
        top: _page(
            [_bar("2026-03-26T09:01:00+09:00"), _bar("2026-03-24T15:30:00+09:00")],
            "2026-03-24T15:30:00+09:00",
        ),
    }
    handler, calls = _paged_handler(pages)
    bars = await _client(handler).collect_minute_candles(
        "005930",
        adjusted=False,
        before=top,
        not_before=datetime(2026, 3, 25, tzinfo=KST),
    )
    assert calls == [top]
    assert [b.timestamp.day for b in bars] == [26]


@pytest.mark.asyncio
async def test_non_advancing_cursor_is_a_contract_error():
    top = "2026-03-26T20:00:00+09:00"
    handler, _ = _paged_handler({top: _page([_bar("2026-03-26T20:00:00+09:00")], top)})
    with pytest.raises(TossResponseContractError, match="non-advancing"):
        await _client(handler).collect_minute_candles(
            "005930",
            adjusted=False,
            before=top,
            not_before=datetime(2026, 3, 25, tzinfo=KST),
        )


@pytest.mark.asyncio
async def test_conflicting_duplicate_bar_is_a_contract_error():
    top = "2026-03-26T20:00:00+09:00"
    pages = {
        top: _page(
            [_bar("2026-03-26T09:02:00+09:00"), _bar("2026-03-26T09:01:00+09:00")],
            "2026-03-26T09:01:00+09:00",
        ),
        "2026-03-26T09:01:00+09:00": _page(
            [_bar("2026-03-26T09:01:00+09:00", price="1")], None
        ),
    }
    handler, _ = _paged_handler(pages)
    with pytest.raises(TossResponseContractError, match="conflicting duplicate"):
        await _client(handler).collect_minute_candles(
            "005930",
            adjusted=False,
            before=top,
            not_before=datetime(2026, 3, 25, tzinfo=KST),
        )


@pytest.mark.asyncio
async def test_bar_newer_than_cursor_is_a_contract_error():
    top = "2026-03-26T20:00:00+09:00"
    handler, _ = _paged_handler({top: _page([_bar("2026-03-26T20:01:00+09:00")], None)})
    with pytest.raises(TossResponseContractError):
        await _client(handler).collect_minute_candles(
            "005930",
            adjusted=False,
            before=top,
            not_before=datetime(2026, 3, 25, tzinfo=KST),
        )


@pytest.mark.asyncio
async def test_page_cap_raises_instead_of_truncating():
    def handler(request):
        before = datetime.fromisoformat(_query(request)["before"])
        older = before - timedelta(minutes=1)
        return httpx.Response(
            200, json=_page([_bar(before.isoformat())], older.isoformat())
        )

    with pytest.raises(TossPaginationCapExceeded):
        await _client(handler).collect_minute_candles(
            "005930",
            adjusted=False,
            before="2026-03-26T20:00:00+09:00",
            not_before=datetime(2026, 3, 25, tzinfo=KST),
            max_pages=3,
        )
    assert issubclass(TossPaginationCapExceeded, ValueError)


@pytest.mark.asyncio
async def test_existing_loose_candles_method_is_unchanged():
    def handler(request):
        return httpx.Response(
            200, json=_page([_bar("2026-03-25T09:32:00+09:00")], None)
        )

    page = await _client(handler).candles("005930", interval="1m")
    assert page.candles[0].timestamp == "2026-03-25T09:32:00+09:00"
