from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest

from research.toss_phase2.collect import (
    AFTER_CLOSE_TARGET_TPS_MIN,
    CONSECUTIVE_429_STOP_AT,
    CONSECUTIVE_TRANSIENT_RESUME_STOP_AT,
    INTRADAY_CHART_HEADROOM_TPS,
    INTRADAY_TARGET_TPS_MAX,
    KST,
    SUMMARY_UPDATE_INTERVAL_SECONDS,
    CollectionStats,
    CollectionStopped,
    HeaderAdaptiveChartRateLimiter,
    LatestSummaryWriter,
    ProgressLog,
    TransientResumeBackoff,
    _preflight_cached_token,
    collect,
)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.monotonic_now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.monotonic_now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.monotonic_now += seconds


class _BaseLimiter:
    def __init__(self) -> None:
        self.groups: list[object] = []

    async def acquire(self, group: object) -> None:
        self.groups.append(group)


def _headers(
    *,
    limit: int | None,
    remaining: int | None,
    reset_seconds: float | None,
    retry_after_seconds: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        limit=limit,
        remaining=remaining,
        reset_seconds=reset_seconds,
        retry_after_seconds=retry_after_seconds,
    )


@pytest.mark.asyncio
async def test_intraday_target_tracks_discovered_cap_and_preserves_headroom() -> None:
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    base = _BaseLimiter()
    pacer = HeaderAdaptiveChartRateLimiter(
        base,
        "MARKET_DATA_CHART",
        now_kst_fn=lambda: clock.now,
        monotonic_fn=clock.monotonic,
        sleep=clock.sleep,
        jitter_fn=lambda _low, _high: 0.0,
    )

    pacer.observe_response(
        "MARKET_DATA_CHART",
        200,
        _headers(limit=4, remaining=4, reset_seconds=0.25),
    )

    assert pacer.cap_auto_discovered is True
    assert pacer.cap == 4
    assert pacer.target_tps() == 1.0
    assert pacer.intraday_headroom_preserved() is True
    assert pacer.cap - pacer.target_tps() >= INTRADAY_CHART_HEADROOM_TPS
    assert pacer.target_tps() <= INTRADAY_TARGET_TPS_MAX

    await pacer.acquire("MARKET_DATA_CHART")
    assert base.groups == []


@pytest.mark.asyncio
async def test_low_remaining_uses_reset_window_before_after_close_request() -> None:
    clock = _Clock(datetime(2026, 8, 4, 20, 1, tzinfo=KST))
    base = _BaseLimiter()
    pacer = HeaderAdaptiveChartRateLimiter(
        base,
        "MARKET_DATA_CHART",
        now_kst_fn=lambda: clock.now,
        monotonic_fn=clock.monotonic,
        sleep=clock.sleep,
        jitter_fn=lambda _low, _high: 0.0,
    )
    pacer.observe_response(
        "MARKET_DATA_CHART",
        200,
        _headers(limit=10, remaining=3, reset_seconds=0.5),
    )

    assert pacer.target_tps() == AFTER_CLOSE_TARGET_TPS_MIN
    await pacer.acquire("MARKET_DATA_CHART")

    # 40% of ten tokens is four. Recovering from three to leave the bucket at
    # that floor after the next request requires two documented Reset windows.
    assert clock.sleeps == [1.0]
    assert base.groups == []


@pytest.mark.asyncio
async def test_startup_probe_never_uses_the_static_chart_limiter() -> None:
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    base = _BaseLimiter()
    pacer = HeaderAdaptiveChartRateLimiter(
        base,
        "MARKET_DATA_CHART",
        now_kst_fn=lambda: clock.now,
        monotonic_fn=clock.monotonic,
        sleep=clock.sleep,
    )

    await pacer.acquire("MARKET_DATA_CHART")

    assert base.groups == []


@pytest.mark.asyncio
async def test_429_honors_retry_after_then_uses_exponential_backoff_and_stops_at_five() -> (
    None
):
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    pacer = HeaderAdaptiveChartRateLimiter(
        _BaseLimiter(),
        "MARKET_DATA_CHART",
        now_kst_fn=lambda: clock.now,
        monotonic_fn=clock.monotonic,
        sleep=clock.sleep,
        jitter_fn=lambda _low, _high: 0.0,
    )

    pacer.observe_response(
        "MARKET_DATA_CHART",
        429,
        _headers(limit=7, remaining=0, reset_seconds=0.25, retry_after_seconds=3.0),
    )
    assert await pacer.backoff_after_429() == (1, 3.0)

    pacer.observe_response(
        "MARKET_DATA_CHART",
        429,
        _headers(limit=7, remaining=0, reset_seconds=0.25),
    )
    assert await pacer.backoff_after_429() == (2, 2.0)
    pacer.observe_response(
        "MARKET_DATA_CHART",
        429,
        _headers(limit=7, remaining=0, reset_seconds=0.25),
    )
    assert await pacer.backoff_after_429() == (3, 4.0)
    pacer.observe_response(
        "MARKET_DATA_CHART",
        429,
        _headers(limit=7, remaining=0, reset_seconds=0.25),
    )
    assert await pacer.backoff_after_429() == (4, 8.0)
    pacer.observe_response(
        "MARKET_DATA_CHART",
        429,
        _headers(limit=7, remaining=0, reset_seconds=0.25),
    )
    with pytest.raises(
        CollectionStopped,
        match=f"consecutive_chart_429:{CONSECUTIVE_429_STOP_AT}",
    ):
        await pacer.backoff_after_429()

    assert clock.sleeps == [3.0, 2.0, 4.0, 8.0]


def test_missing_cap_fails_closed_after_the_startup_response() -> None:
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    pacer = HeaderAdaptiveChartRateLimiter(
        _BaseLimiter(),
        "MARKET_DATA_CHART",
        now_kst_fn=lambda: clock.now,
        monotonic_fn=clock.monotonic,
        sleep=clock.sleep,
    )
    pacer.observe_response(
        "MARKET_DATA_CHART",
        200,
        _headers(limit=None, remaining=None, reset_seconds=None),
    )

    with pytest.raises(CollectionStopped, match="cap_not_discovered"):
        pacer.ensure_cap_discovered()


@pytest.mark.asyncio
async def test_collect_retries_a_429_without_stopping_the_pipeline(tmp_path) -> None:
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    pacer = HeaderAdaptiveChartRateLimiter(
        _BaseLimiter(),
        "MARKET_DATA_CHART",
        now_kst_fn=lambda: clock.now,
        monotonic_fn=clock.monotonic,
        sleep=clock.sleep,
        jitter_fn=lambda _low, _high: 0.0,
    )
    transport = SimpleNamespace(calls=0)

    class _RateLimitError(RuntimeError):
        status_code = 429

    class _Client:
        async def candles(self, *args, **kwargs):
            del args, kwargs
            transport.calls += 1
            if transport.calls == 1:
                pacer.observe_response(
                    "MARKET_DATA_CHART",
                    429,
                    _headers(
                        limit=4,
                        remaining=0,
                        reset_seconds=0.25,
                        retry_after_seconds=2.0,
                    ),
                )
                raise _RateLimitError()
            pacer.observe_response(
                "MARKET_DATA_CHART",
                200,
                _headers(limit=4, remaining=4, reset_seconds=0.25),
            )
            return SimpleNamespace(candles=[], next_before=None)

    class _Monitor:
        async def assert_healthy(self) -> None:
            return None

    progress = ProgressLog(tmp_path / "events" / "progress.jsonl")
    try:
        stats = await collect(
            client=_Client(),
            transport=transport,
            chart_pacer=pacer,
            monitor=_Monitor(),
            staging_dir=tmp_path,
            symbols=["005930"],
            start_date=date(2021, 12, 20),
            last_eligible_date=date(2026, 8, 3),
            call_budget=10,
            batch_id="batch",
            official_nxt_launch_date=None,
            progress=progress,
            stats=CollectionStats(symbols_total=1),
        )
    finally:
        progress.close()

    assert transport.calls == 2
    assert stats.http_429 == 1
    assert stats.rate_limit_backoffs == 1
    assert stats.symbols_done == 1
    assert stats.stopped_reason is None
    assert clock.sleeps == [2.0]


@pytest.mark.asyncio
async def test_startup_429_without_limit_header_stops_before_a_second_request(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    pacer = HeaderAdaptiveChartRateLimiter(
        _BaseLimiter(),
        "MARKET_DATA_CHART",
        now_kst_fn=lambda: clock.now,
        monotonic_fn=clock.monotonic,
        sleep=clock.sleep,
    )
    transport = SimpleNamespace(calls=0)

    class _RateLimitError(RuntimeError):
        status_code = 429

    class _Client:
        async def candles(self, *args, **kwargs):
            del args, kwargs
            transport.calls += 1
            pacer.observe_response(
                "MARKET_DATA_CHART",
                429,
                _headers(limit=None, remaining=0, reset_seconds=0.25),
            )
            raise _RateLimitError()

    class _Monitor:
        async def assert_healthy(self) -> None:
            return None

    progress = ProgressLog(tmp_path / "events" / "progress.jsonl")
    try:
        with pytest.raises(CollectionStopped, match="cap_not_discovered"):
            await collect(
                client=_Client(),
                transport=transport,
                chart_pacer=pacer,
                monitor=_Monitor(),
                staging_dir=tmp_path,
                symbols=["005930"],
                start_date=date(2021, 12, 20),
                last_eligible_date=date(2026, 8, 3),
                call_budget=10,
                batch_id="batch",
                official_nxt_launch_date=None,
                progress=progress,
                stats=CollectionStats(symbols_total=1),
            )
    finally:
        progress.close()

    assert transport.calls == 1


@pytest.mark.asyncio
async def test_collect_retries_shared_cache_gap_on_the_same_cursor(tmp_path) -> None:
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    pacer = HeaderAdaptiveChartRateLimiter(
        _BaseLimiter(),
        "MARKET_DATA_CHART",
        now_kst_fn=lambda: clock.now,
        monotonic_fn=clock.monotonic,
        sleep=clock.sleep,
        jitter_fn=lambda _low, _high: 0.0,
    )
    transport = SimpleNamespace(calls=0)

    class _Client:
        attempts = 0

        async def candles(self, *args, **kwargs):
            del args, kwargs
            self.attempts += 1
            if self.attempts == 1:
                raise CollectionStopped(
                    "shared_toss_token_cache_miss: collector will not issue OAuth"
                )
            transport.calls += 1
            pacer.observe_response(
                "MARKET_DATA_CHART",
                200,
                _headers(limit=4, remaining=4, reset_seconds=0.25),
            )
            return SimpleNamespace(candles=[], next_before=None)

    class _Monitor:
        async def assert_healthy(self) -> None:
            return None

    progress = ProgressLog(tmp_path / "events" / "progress.jsonl")
    try:
        stats = await collect(
            client=_Client(),
            transport=transport,
            chart_pacer=pacer,
            monitor=_Monitor(),
            staging_dir=tmp_path,
            symbols=["005930"],
            start_date=date(2021, 12, 20),
            last_eligible_date=date(2026, 8, 3),
            call_budget=10,
            batch_id="batch",
            official_nxt_launch_date=None,
            progress=progress,
            stats=CollectionStats(symbols_total=1),
            transient_resumer=TransientResumeBackoff(
                sleep=clock.sleep,
                jitter_fn=lambda _low, _high: 0.0,
            ),
        )
    finally:
        progress.close()

    assert transport.calls == 1
    assert stats.transient_resume_failures == 1
    assert stats.transient_resume_backoffs == 1
    assert stats.symbols_done == 1
    assert clock.sleeps == [1.0]


@pytest.mark.asyncio
async def test_collect_retries_one_transport_timeout_on_the_same_cursor(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    pacer = HeaderAdaptiveChartRateLimiter(
        _BaseLimiter(),
        "MARKET_DATA_CHART",
        now_kst_fn=lambda: clock.now,
        monotonic_fn=clock.monotonic,
        sleep=clock.sleep,
        jitter_fn=lambda _low, _high: 0.0,
    )
    transport = SimpleNamespace(calls=0)

    class _Client:
        async def candles(self, *args, **kwargs):
            del args, kwargs
            transport.calls += 1
            if transport.calls == 1:
                raise httpx.ReadTimeout("one-off timeout")
            pacer.observe_response(
                "MARKET_DATA_CHART",
                200,
                _headers(limit=4, remaining=4, reset_seconds=0.25),
            )
            return SimpleNamespace(candles=[], next_before=None)

    class _Monitor:
        async def assert_healthy(self) -> None:
            return None

    progress = ProgressLog(tmp_path / "events" / "progress.jsonl")
    try:
        stats = await collect(
            client=_Client(),
            transport=transport,
            chart_pacer=pacer,
            monitor=_Monitor(),
            staging_dir=tmp_path,
            symbols=["005930"],
            start_date=date(2021, 12, 20),
            last_eligible_date=date(2026, 8, 3),
            call_budget=10,
            batch_id="batch",
            official_nxt_launch_date=None,
            progress=progress,
            stats=CollectionStats(symbols_total=1),
            transient_resumer=TransientResumeBackoff(
                sleep=clock.sleep,
                jitter_fn=lambda _low, _high: 0.0,
            ),
        )
    finally:
        progress.close()

    assert transport.calls == 2
    assert stats.transient_resume_failures == 1
    assert stats.transient_resume_backoffs == 1
    assert stats.symbols_done == 1
    assert clock.sleeps == [1.0]


@pytest.mark.asyncio
async def test_preflight_retries_only_a_shared_cache_gap(tmp_path) -> None:
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    transport = SimpleNamespace(calls=0)

    class _Provider:
        attempts = 0

        async def get_access_token(self, **kwargs) -> str:
            del kwargs
            self.attempts += 1
            if self.attempts == 1:
                raise CollectionStopped(
                    "shared_toss_token_cache_miss: collector will not issue OAuth"
                )
            return "cached-token"

    progress = ProgressLog(tmp_path / "events" / "progress.jsonl")
    stats = CollectionStats(symbols_total=1)
    try:
        await _preflight_cached_token(
            token_provider=_Provider(),
            transient_resumer=TransientResumeBackoff(
                sleep=clock.sleep,
                jitter_fn=lambda _low, _high: 0.0,
            ),
            progress=progress,
            stats=stats,
            transport=transport,
            on_progress=None,
        )
    finally:
        progress.close()

    assert stats.transient_resume_failures == 1
    assert stats.transient_resume_backoffs == 1
    assert transport.calls == 0
    assert clock.sleeps == [1.0]


@pytest.mark.asyncio
async def test_transient_resume_stops_at_five_consecutive_failures() -> None:
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    resumer = TransientResumeBackoff(
        sleep=clock.sleep,
        jitter_fn=lambda _low, _high: 0.0,
    )

    for expected in (1.0, 2.0, 4.0, 8.0):
        _consecutive, delay = await resumer.backoff(reason="httpx_transport_error")
        assert delay == expected

    with pytest.raises(
        CollectionStopped,
        match=(
            "transient_resume_exhausted:reason=httpx_transport_error:"
            f"stop_at={CONSECUTIVE_TRANSIENT_RESUME_STOP_AT}"
        ),
    ):
        await resumer.backoff(reason="httpx_transport_error")

    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0]


def test_latest_summary_refreshes_atomically_while_running(tmp_path) -> None:
    clock = _Clock(datetime(2026, 8, 4, 10, 0, tzinfo=KST))
    transport = SimpleNamespace(calls=3)
    stats = CollectionStats(symbols_total=200, rows_staged=10, pages_staged=1)
    summary_path = tmp_path / "events" / "latest_summary.json"
    writer = LatestSummaryWriter(
        path=summary_path,
        stats=stats,
        transport=transport,
        call_budget=820000,
        monotonic_fn=clock.monotonic,
        now_kst_fn=lambda: clock.now,
    )

    writer.write(collector_state="RUNNING")
    initial = json.loads(summary_path.read_text())
    assert initial["collector_state"] == "RUNNING"
    assert initial["calls_actual"] == 3
    assert initial["database_load_performed"] is False

    clock.monotonic_now += SUMMARY_UPDATE_INTERVAL_SECONDS - 0.1
    writer.maybe_write()
    assert json.loads(summary_path.read_text()) == initial

    transport.calls = 4
    clock.monotonic_now += 0.2
    writer.maybe_write()
    refreshed = json.loads(summary_path.read_text())
    assert refreshed["calls_actual"] == 4
    assert refreshed["collector_state"] == "RUNNING"
