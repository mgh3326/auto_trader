"""hk 6508: streaming collection — each page persists as it completes; one bad page never aborts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.fnguide_consensus.client import FetchError, FetchResult
from app.services.fnguide_consensus.collector import collect_pages
from tests.services.fnguide_consensus._helpers import OBSERVED, load_fixture

pytestmark = pytest.mark.unit

FIXTURE = {
    ("005930", "Consensus"): "005930_consensus.html",
    ("000660", "Consensus"): "000660_consensus.html",
}


class FakeClient:
    """Duck-typed stand-in for FnguideClient.fetch_page."""

    def __init__(self, behaviour: dict[tuple[str, str], object]) -> None:
        self._behaviour = behaviour

    async def fetch_page(self, page: str, code: str) -> FetchResult:
        outcome = self._behaviour.get((code, page))
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            outcome = load_fixture(FIXTURE[(code, page)])
        return FetchResult(
            url=f"https://wcomp.fnguide.com/{page}?cmp_cd={code}",
            text=outcome,
            fetched_at=datetime(2026, 11, 25, 7, 0, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_each_parsed_page_is_handed_to_the_sink_and_not_retained() -> None:
    received: list[str] = []

    async def sink(parsed) -> None:
        received.append(f"{parsed.symbol}:{parsed.page}")

    result = await collect_pages(
        FakeClient({}), ["005930", "000660"], ("Consensus",), OBSERVED, on_parsed=sink
    )

    assert sorted(received) == ["000660:Consensus", "005930:Consensus"]
    assert len(result.summaries) == 2
    assert result.failures == []


@pytest.mark.asyncio
async def test_unexpected_exception_is_a_closed_failure_and_other_pages_continue() -> (
    None
):
    client = FakeClient({("000660", "Consensus"): RuntimeError("boom with a body")})
    result = await collect_pages(client, ["005930", "000660"], ("Consensus",), OBSERVED)

    assert [s["symbol"] for s in result.summaries] == ["005930"]
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.kind == "unexpected"
    assert "boom" not in failure.kind


@pytest.mark.asyncio
async def test_fetch_error_kind_and_status_are_kept() -> None:
    client = FakeClient({("000660", "Consensus"): FetchError("http_4xx", 404)})
    result = await collect_pages(client, ["000660"], ("Consensus",), OBSERVED)

    assert result.summaries == []
    assert result.failures[0].kind == "http_4xx"
    assert result.failures[0].http_status == 404


@pytest.mark.asyncio
async def test_a_sink_failure_stops_the_run_only_through_the_caller() -> None:
    async def sink(parsed) -> None:
        raise ValueError("write failed")

    with pytest.raises(ValueError):
        await collect_pages(
            FakeClient({}), ["005930"], ("Consensus",), OBSERVED, on_parsed=sink
        )


@pytest.mark.asyncio
async def test_sink_abort_cancels_remaining_fetches_and_keeps_the_partial_result() -> (
    None
):
    from app.services.fnguide_consensus.collector import SinkAborted

    calls = 0

    async def sink(parsed) -> None:
        nonlocal calls
        calls += 1
        raise SinkAborted("consecutive_write_failures")

    with pytest.raises(SinkAborted) as excinfo:
        await collect_pages(
            FakeClient({}),
            ["005930", "000660"],
            ("Consensus",),
            OBSERVED,
            on_parsed=sink,
        )
    assert excinfo.value.reason == "consecutive_write_failures"
    assert excinfo.value.partial is not None
    assert calls == 1
