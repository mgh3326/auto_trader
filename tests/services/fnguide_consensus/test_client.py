"""hk 6508: polite FnGuide client — host, redirects, retries, pacing (no network)."""

from __future__ import annotations

import httpx
import pytest

from app.services.fnguide_consensus.client import (
    ALLOWED_HOST,
    MAX_CONCURRENCY,
    FetchError,
    FnguideClient,
    build_url,
)

pytestmark = pytest.mark.unit

HTML_OK = "<html><title>삼성전자(005930) | 컨센서스</title></html>"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(handler, clock: FakeClock | None = None, **kwargs) -> FnguideClient:
    clock = clock or FakeClock()
    return FnguideClient(
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock.clock,
        **kwargs,
    )


def test_url_uses_the_allowed_host_and_rejects_bad_codes() -> None:
    assert build_url("Consensus", "005930") == (
        f"https://{ALLOWED_HOST}/CompanyInfo/Consensus?cmp_cd=005930"
    )
    with pytest.raises(ValueError):
        build_url("Consensus", "5930")
    with pytest.raises(ValueError):
        build_url("Consensus", "A005930")


def test_concurrency_above_two_is_refused() -> None:
    with pytest.raises(ValueError):
        FnguideClient(concurrency=MAX_CONCURRENCY + 1)


@pytest.mark.asyncio
async def test_success_returns_text_and_the_request_is_generic_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=HTML_OK)

    async with _client(handler, min_interval=0.0) as client:
        result = await client.fetch_page("Consensus", "005930")

    assert "삼성전자" in result.text
    assert seen[0].url.host == ALLOWED_HOST
    assert seen[0].headers["user-agent"] == "at-research-collector/1.0"


@pytest.mark.asyncio
async def test_redirect_is_recorded_not_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "https://elsewhere.example/x"})

    async with _client(handler, min_interval=0.0) as client:
        with pytest.raises(FetchError) as excinfo:
            await client.fetch_page("Consensus", "005930")

    assert excinfo.value.kind == "redirect_refused"
    assert calls == 1


@pytest.mark.asyncio
async def test_client_error_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with _client(handler, min_interval=0.0, retries=3) as client:
        with pytest.raises(FetchError) as excinfo:
            await client.fetch_page("Consensus", "005930")

    assert excinfo.value.kind == "http_4xx"
    assert excinfo.value.status == 404
    assert calls == 1


@pytest.mark.asyncio
async def test_server_error_is_retried_then_succeeds() -> None:
    responses = [httpx.Response(503), httpx.Response(200, text=HTML_OK)]
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with _client(handler, clock, min_interval=0.0, retries=2) as client:
        result = await client.fetch_page("Consensus", "005930")

    assert "삼성전자" in result.text
    assert len(clock.sleeps) >= 1


@pytest.mark.asyncio
async def test_timeouts_stop_after_the_retry_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    async with _client(handler, min_interval=0.0, retries=2) as client:
        with pytest.raises(FetchError) as excinfo:
            await client.fetch_page("Consensus", "005930")

    assert excinfo.value.kind == "timeout"
    assert calls == 3


@pytest.mark.asyncio
async def test_pacing_spaces_request_starts_by_the_interval() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HTML_OK)

    async with _client(handler, clock, min_interval=1.0) as client:
        await client.fetch_page("Consensus", "005930")
        await client.fetch_page("Invest", "005930")
        await client.fetch_page("Finance", "005930")

    assert clock.sleeps == [1.0, 1.0]


@pytest.mark.asyncio
async def test_other_hosts_are_refused_before_sending() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=HTML_OK)

    async with _client(handler, min_interval=0.0) as client:
        with pytest.raises(FetchError) as excinfo:
            await client._get_once(
                "https://comp.fnguide.com/CompanyInfo/Consensus?cmp_cd=005930"
            )

    assert excinfo.value.kind == "host_refused"
    assert calls == 0
