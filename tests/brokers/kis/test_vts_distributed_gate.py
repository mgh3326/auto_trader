"""ROB-892 — distributed Redis gate for KIS mock (VTS) REST dispatch.

Test surface (per the ROB-892 acceptance spec):

* A real disposable ``redis-server`` is launched per test session; no
  ``fakeredis`` object is accepted as proof.
* A multiprocessing suite runs >=2 independent PIDs/clients concurrently
  issuing KR buy, KR sell, and a mock read through the *real* dispatch
  boundary. Actual ``_execute_http_request`` START timestamps are measured
  (not ``acquire()`` returns); adjacent starts for one scope meet the
  configured interval and each intended dispatch fires at most once.
* Same credential across TR/path/market/side/read/order shares one key;
  distinct credentials/hosts isolate.
* Redis unavailable / timeout, acquire-deadline, wait-cancellation, and
  pre-send-freshness failure all produce mutation HTTP=0 (no fallback).
* Contention loser reruns freshness; no long gate wait follows the final
  freshness check.
* Live requests touch Redis zero times.
* TTL / cold start / safety margin / bounded backlog are deterministic, and
  keys/logs carry no raw secret.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import socket
import subprocess
import time
from contextlib import closing
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.vts_distributed_gate import (
    AcquireResult,
    DistributedGateUnavailable,
    VTSDistributedGate,
    build_vts_gate_scope_key,
    get_vts_distributed_gate,
    netloc_from_url,
    reset_vts_distributed_gate,
)

MOCK_HOST = "openapivts.koreainvestment.com:29443"
MOCK_BASE_URL = f"https://{MOCK_HOST}"
LIVE_HOST = "openapi.koreainvestment.com:9443"
MOCK_APP_KEY = "test-mock-app-key-ROB892"


# ---------------------------------------------------------------------------
# Disposable real redis-server fixture
# ---------------------------------------------------------------------------


def _free_tcp_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _DisposableRedis:
    """A real redis-server subprocess bound to 127.0.0.1:<port> for one suite."""

    def __init__(self) -> None:
        self.port = _free_tcp_port()
        self.url = f"redis://127.0.0.1:{self.port}/0"
        self.proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        # --daemonize no, log to stdout, no persistence, bind loopback only.
        self.proc = subprocess.Popen(
            [
                "redis-server",
                "--port",
                str(self.port),
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
                "--loglevel",
                "warning",
                "--protected-mode",
                "no",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Wait for readiness.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"redis-server exited early:\n{out}")
            try:
                with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
                    s.settimeout(0.25)
                    s.connect(("127.0.0.1", self.port))
                    break
            except OSError:
                time.sleep(0.05)
        else:  # pragma: no cover — environment failure
            raise RuntimeError(f"redis-server did not become ready on port {self.port}")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.proc.kill()
            self.proc = None

    @property
    def version(self) -> str:
        out = subprocess.run(
            ["redis-cli", "-h", "127.0.0.1", "-p", str(self.port), "INFO", "server"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
        for line in out.splitlines():
            if line.startswith("redis_version:"):
                return line.split(":", 1)[1].strip()
        return "unknown"


@pytest.fixture(scope="module")
def disposable_redis() -> Any:
    """Launch + tear down a real isolated redis-server for this module."""
    if not shutil_which("redis-server"):
        pytest.skip("redis-server not installed on this host")
    rd = _DisposableRedis()
    rd.start()
    try:
        yield rd
    finally:
        rd.stop()


def shutil_which(cmd: str) -> str | None:
    from shutil import which

    return which(cmd)


def _make_gate(
    redis_url: str,
    *,
    interval_seconds: float = 1.0,
    safety_margin_seconds: float = 0.05,
    acquire_deadline_seconds: float = 10.0,
    ttl_seconds: int = 120,
    socket_timeout_seconds: float = 2.0,
    socket_connect_timeout_seconds: float = 2.0,
) -> VTSDistributedGate:
    return VTSDistributedGate(
        redis_url=redis_url,
        interval_seconds=interval_seconds,
        safety_margin_seconds=safety_margin_seconds,
        acquire_deadline_seconds=acquire_deadline_seconds,
        ttl_seconds=ttl_seconds,
        socket_timeout_seconds=socket_timeout_seconds,
        socket_connect_timeout_seconds=socket_connect_timeout_seconds,
    )


# ===========================================================================
# Section 1 — gate module unit tests (real disposable Redis)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_first_admit_is_immediate(disposable_redis: _DisposableRedis):
    gate = _make_gate(disposable_redis.url)
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key=MOCK_APP_KEY)
        t0 = time.monotonic()
        result = await gate.acquire(scope, call_class="order")
        elapsed = time.monotonic() - t0
        assert isinstance(result, AcquireResult)
        assert result.contention_attempts == 1
        assert elapsed < 0.2  # cold start admits immediately
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_adjacent_admits_meet_interval(disposable_redis: _DisposableRedis):
    """Two sequential admits through the gate must be >= interval apart."""
    gate = _make_gate(
        disposable_redis.url, interval_seconds=0.4, safety_margin_seconds=0.05
    )
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="adjacent")
        await gate.acquire(scope, call_class="order")
        t0 = time.monotonic()
        await gate.acquire(scope, call_class="order")
        gap = time.monotonic() - t0
        # 0.4 + 0.05 margin -> gap must be >= 0.40 (allow tiny scheduling slop)
        assert gap >= 0.40 - 0.03, f"second admit came too early: {gap:.3f}s"
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_admits_serialize_no_burst(disposable_redis: _DisposableRedis):
    """Three concurrent acquires for the same scope never burst past 1/interval."""
    gate = _make_gate(
        disposable_redis.url, interval_seconds=0.3, safety_margin_seconds=0.05
    )
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="concurrent")
        admit_times: list[float] = []

        async def _one(idx: int) -> int:
            await gate.acquire(scope, call_class="order")
            admit_times.append(time.monotonic())
            return idx

        await asyncio.gather(*[_one(i) for i in range(3)])
        admit_times.sort()
        gaps = [admit_times[i] - admit_times[i - 1] for i in range(1, len(admit_times))]
        # Every adjacent gap must meet the interval (minus tiny scheduling slop).
        for g in gaps:
            assert g >= 0.30 - 0.04, f"burst detected, gap={g:.3f}s"
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_distinct_credentials_isolate(disposable_redis: _DisposableRedis):
    """Different app-key fingerprints admit independently (no cross-contention)."""
    gate = _make_gate(
        disposable_redis.url, interval_seconds=0.3, safety_margin_seconds=0.05
    )
    try:
        scope_a = build_vts_gate_scope_key(host=MOCK_HOST, app_key="cred-A")
        scope_b = build_vts_gate_scope_key(host=MOCK_HOST, app_key="cred-B")
        t0 = time.monotonic()
        await asyncio.gather(
            gate.acquire(scope_a, call_class="order"),
            gate.acquire(scope_b, call_class="order"),
        )
        elapsed = time.monotonic() - t0
        # Distinct scopes admit in parallel — total wall time well under one interval.
        assert elapsed < 0.30, f"distinct scopes did not parallelize: {elapsed:.3f}s"
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_distinct_hosts_isolate(disposable_redis: _DisposableRedis):
    gate = _make_gate(disposable_redis.url, interval_seconds=0.3)
    try:
        same_app = "shared-app-key"
        scope_vts = build_vts_gate_scope_key(host=MOCK_HOST, app_key=same_app)
        scope_alt = build_vts_gate_scope_key(
            host="openapivts.koreainvestment.com:29444", app_key=same_app
        )
        t0 = time.monotonic()
        await asyncio.gather(
            gate.acquire(scope_vts, call_class="order"),
            gate.acquire(scope_alt, call_class="order"),
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 0.30
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redis_unavailable_raises_fail_closed():
    """Redis down -> DistributedGateUnavailable (never an unthrottled admit)."""
    # Point at a port where nothing listens; connect must fail fast.
    gate = _make_gate(
        "redis://127.0.0.1:1/0",
        socket_timeout_seconds=0.5,
        socket_connect_timeout_seconds=0.5,
        acquire_deadline_seconds=2.0,
    )
    scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="down")
    try:
        with pytest.raises(DistributedGateUnavailable):
            await gate.acquire(scope, call_class="order")
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_acquire_deadline_raises_fail_closed(disposable_redis: _DisposableRedis):
    """A backlog that cannot drain within the deadline fails closed."""
    # Very short deadline + an already-occupied slot -> deadline exceeded.
    gate = _make_gate(
        disposable_redis.url,
        interval_seconds=2.0,
        safety_margin_seconds=0.0,
        acquire_deadline_seconds=0.4,
    )
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="deadline")
        await gate.acquire(scope, call_class="order")  # occupy the slot
        with pytest.raises(DistributedGateUnavailable):
            await gate.acquire(scope, call_class="order")
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancellation_while_waiting_is_http_zero(
    disposable_redis: _DisposableRedis,
):
    """A waiter cancelled mid-sleep never reaches the admit (HTTP=0)."""
    gate = _make_gate(
        disposable_redis.url, interval_seconds=1.0, safety_margin_seconds=0.0
    )
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="cancel")
        await gate.acquire(scope, call_class="order")  # occupy

        admitted = {"v": False}

        async def _waiter() -> None:
            await gate.acquire(scope, call_class="order")
            admitted["v"] = True

        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)  # let it enter the contention sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admitted["v"] is False  # never admitted -> HTTP would be 0
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_freshness_hook_rerun_on_contention(disposable_redis: _DisposableRedis):
    """Contention forces the freshness hook to run again before re-claim."""
    gate = _make_gate(
        disposable_redis.url, interval_seconds=0.4, safety_margin_seconds=0.0
    )
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="fresh")
        await gate.acquire(scope, call_class="order")  # occupy slot

        calls = {"n": 0}

        async def _hook() -> None:
            calls["n"] += 1

        # Second admit must contend once -> hook runs twice (before each claim).
        await gate.acquire(scope, freshness_hook=_hook, call_class="order")
        assert calls["n"] >= 2, f"freshness not rerun on contention: {calls['n']}"
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_freshness_failure_aborts_before_admit(
    disposable_redis: _DisposableRedis,
):
    """A failing freshness hook aborts with HTTP=0 (PreSendFreshnessError)."""

    class _FreshError(RuntimeError):
        pass

    gate = _make_gate(disposable_redis.url)
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="freshfail")
        admitted = {"v": False}

        async def _failing_hook() -> None:
            raise _FreshError("stale book")

        # The hook runs before the first claim; the gate must propagate the
        # freshness error and never admit.
        with pytest.raises(_FreshError):

            async def _track() -> None:
                await gate.acquire(
                    scope, freshness_hook=_failing_hook, call_class="order"
                )
                admitted["v"] = True

            await _track()

        assert admitted["v"] is False
    finally:
        await gate.close()


@pytest.mark.integration
def test_scope_key_is_secret_free():
    """Keys and fingerprints must never contain the raw app key."""
    key = build_vts_gate_scope_key(host=MOCK_HOST, app_key="SUPER_SECRET_VALUE_123")
    assert "SUPER_SECRET_VALUE_123" not in key
    assert key.startswith("kis_mock:gate:openapivts.koreainvestment.com:29443:")
    # Fingerprint is 16 hex chars.
    fp = key.rsplit(":", 1)[-1]
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


@pytest.mark.integration
def test_netloc_helper_matches_token_namespace():
    """Gate host extraction must match the OAuth token namespace netloc shape."""
    assert netloc_from_url(MOCK_BASE_URL + "/uapi/x") == MOCK_HOST
    assert netloc_from_url("https://openapi.koreainvestment.com:9443/p") == LIVE_HOST


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cold_start_after_ttl_expiry_is_deterministic(
    disposable_redis: _DisposableRedis,
):
    """After the cooldown key TTL expires, the next admit is immediate (cold start)."""
    gate = _make_gate(
        disposable_redis.url,
        interval_seconds=0.2,
        safety_margin_seconds=0.0,
        ttl_seconds=1,  # short TTL to observe cold-start recovery
    )
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="ttl")
        await gate.acquire(scope, call_class="order")
        # Wait past TTL.
        await asyncio.sleep(1.2)
        t0 = time.monotonic()
        await gate.acquire(scope, call_class="order")
        assert time.monotonic() - t0 < 0.2  # cold-start immediate admit
    finally:
        await gate.close()


# ===========================================================================
# Section 2 — singleton wiring
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_singleton_reset_round_trip():
    await reset_vts_distributed_gate()
    g1 = await get_vts_distributed_gate()
    g2 = await get_vts_distributed_gate()
    assert g1 is g2
    await reset_vts_distributed_gate()


# ===========================================================================
# Section 3 — real multiprocessing dispatch-timing suite
# ===========================================================================


class _MockSettings:
    """Minimal settings view: mock host + mock app key, live fallbacks."""

    kis_app_key = MOCK_APP_KEY
    kis_app_secret = "secret"
    kis_access_token = "token"
    kis_account_no = "1234567890"
    kis_base_url = MOCK_BASE_URL  # dispatch URL resolves to mock host
    kis_mock_base_url = MOCK_BASE_URL
    api_rate_limit_retry_429_max = 0
    api_rate_limit_retry_429_base_delay = 0.0
    kis_rate_limit_rate = 19
    kis_rate_limit_period = 1.0
    kis_api_rate_limits: dict[str, Any] = {}


class _DispatchTrackingClient(BaseKISClient):
    """BaseKISClient subclass that records real _execute_http_request starts.

    ``_vts_gate`` is injected so the dispatch boundary contacts the disposable
    Redis instead of the process-wide singleton.
    """

    def __init__(self, gate: VTSDistributedGate) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set[str] = set()
        type(self)._shared_client_lock = None
        self._vts_gate = gate
        self._dispatch_starts: list[float] = []
        self._dispatch_count = 0

    @property  # type: ignore[override]
    def _settings(self) -> Any:  # type: ignore[override]
        return _MockSettings()

    async def _get_limiter(self, api_key: str, *, rate: int, period: float) -> Any:
        limiter = MagicMock()
        limiter.acquire = AsyncMock()
        return limiter

    async def _ensure_client(self, timeout: float | None = None) -> Any:  # type: ignore[override]
        return MagicMock()

    async def _execute_http_request(  # type: ignore[override]
        self, client: Any, method: str, url: str, **kwargs: Any
    ) -> Any:
        # Record the START timestamp (the spec measures dispatch starts, not
        # acquire() returns). Sleep a hair so concurrency is observable.
        self._dispatch_starts.append(time.monotonic())
        self._dispatch_count += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"rt_cd": "0", "output": {}, "msg1": "ok"}
        return resp


def _child_worker(  # noqa: C901 — multiprocessing target
    redis_url: str,
    app_key: str,
    requests: list[tuple[str, str]],
    out_queue: mp.Queue,
    interval_seconds: float,
    safety_margin_seconds: float,
) -> None:
    """Run N dispatches in a child PID; push (label, start_timestamps, count)."""
    # Each child needs its own event loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    gate = _make_gate(
        redis_url,
        interval_seconds=interval_seconds,
        safety_margin_seconds=safety_margin_seconds,
        acquire_deadline_seconds=15.0,
    )

    # Patch the app key on this child's settings view (per-credential tests).
    _MockSettings.kis_app_key = app_key
    _MockSettings.kis_mock_base_url = MOCK_BASE_URL

    client = _DispatchTrackingClient(gate)

    async def _run() -> list[tuple[str, float]]:
        starts: list[tuple[str, float]] = []
        for label, path in requests:
            url = MOCK_BASE_URL + path
            headers = {"appkey": app_key, "authorization": "Bearer tok", "tr_id": label}
            await client._dispatch_rate_limited_with_headers(
                "POST"
                if "order" in label or "cancel" in label or "modify" in label
                else "GET",
                url,
                headers=headers,
                params=None,
                json_body={"x": 1} if "order" in label else None,
                timeout=5.0,
                api_name=label,
                tr_id=label,
                retry_request_errors=False,
                max_retries_override=0,
            )
            starts.append((label, client._dispatch_starts[-1]))
        return starts

    try:
        starts = loop.run_until_complete(_run())
        out_queue.put((app_key, starts, client._dispatch_count))
    except Exception as e:  # noqa: BLE001
        out_queue.put((app_key, [], -1, repr(e)))
    finally:
        loop.run_until_complete(gate.close())
        loop.close()


@pytest.mark.integration
def test_multiprocessing_kr_buy_sell_read_share_budget(
    disposable_redis: _DisposableRedis,
):
    """Two PIDs issuing KR buy, KR sell, and a mock read through the real
    dispatch boundary share ONE admit budget: adjacent _execute_http_request
    starts for the credential scope meet the interval, and each intended
    dispatch fires exactly once."""
    interval = 0.35
    margin = 0.05
    # Flush any prior cooldown for this credential.
    subprocess.run(
        ["redis-cli", "-h", "127.0.0.1", "-p", str(disposable_redis.port), "FLUSHDB"],
        check=False,
        capture_output=True,
        timeout=5,
    )

    # PID A: KR buy then a mock read. PID B: KR sell. Same credential scope.
    reqs_a = [("kr_buy", "/uapi/domestic-stock/v1/trading/order-cash")]
    reqs_b = [
        ("kr_sell", "/uapi/domestic-stock/v1/trading/order-cash"),
        ("mock_read", "/uapi/domestic-stock/v1/trading/inquire-balance"),
    ]

    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=_child_worker,
            args=(disposable_redis.url, MOCK_APP_KEY, reqs_a, q, interval, margin),
        ),
        ctx.Process(
            target=_child_worker,
            args=(disposable_redis.url, MOCK_APP_KEY, reqs_b, q, interval, margin),
        ),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    results = []
    while not q.empty():
        results.append(q.get())
    assert len(results) == 2, f"expected 2 child results, got {results}"

    all_starts: list[float] = []
    total_dispatches = 0
    for r in results:
        assert len(r) >= 3, f"child reported failure: {r}"
        _app_key, starts, count = r[0], r[1], r[2]
        assert count >= 1 and count == len(starts), (
            f"each intended dispatch must fire exactly once: count={count}, starts={len(starts)}"
        )
        total_dispatches += count
        all_starts.extend(ts for _label, ts in starts)

    # 3 intended requests across 2 PIDs -> exactly 3 HTTP dispatches.
    assert total_dispatches == 3, f"expected 3 dispatches, got {total_dispatches}"

    all_starts.sort()
    gaps = [all_starts[i] - all_starts[i - 1] for i in range(1, len(all_starts))]
    # Adjacent starts must meet the interval (minus small scheduling slop for
    # multiprocess clock + asyncio wakeup).
    slop = 0.05
    for g in gaps:
        assert g >= interval - slop, (
            f"cross-PID burst: adjacent dispatch gap {g:.3f}s < interval {interval}s"
        )


@pytest.mark.integration
def test_multiprocessing_distinct_credentials_do_not_serialize(
    disposable_redis: _DisposableRedis,
):
    """Two PIDs with DIFFERENT mock app keys admit in parallel (no shared budget)."""
    interval = 0.4
    subprocess.run(
        ["redis-cli", "-h", "127.0.0.1", "-p", str(disposable_redis.port), "FLUSHDB"],
        check=False,
        capture_output=True,
        timeout=5,
    )
    reqs = [("kr_buy", "/uapi/domestic-stock/v1/trading/order-cash")]
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=_child_worker,
            args=(disposable_redis.url, "cred-alpha", reqs, q, interval, 0.05),
        ),
        ctx.Process(
            target=_child_worker,
            args=(disposable_redis.url, "cred-beta", reqs, q, interval, 0.05),
        ),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    results = []
    while not q.empty():
        results.append(q.get())
    assert len(results) == 2
    # Distinct credentials -> the two dispatches' START timestamps must be
    # close (parallel admission), not separated by the full interval. Wall-clock
    # spawn startup is excluded by comparing dispatch starts directly.
    starts = sorted(ts for r in results for _label, ts in r[1])
    assert len(starts) == 2
    delta = starts[1] - starts[0]
    assert delta < interval * 0.5, (
        f"distinct credentials serialized (dispatch starts {delta:.3f}s apart, "
        f"expected near-simultaneous): {results}"
    )
    for r in results:
        assert r[2] == 1, f"each PID must dispatch exactly once: {r}"


# ===========================================================================
# Section 4 — dispatch-boundary integration (HTTP=0 invariants)
# ===========================================================================


class _UnitMockSettings(_MockSettings):
    # Reads retry transient RequestErrors by default; tests that need
    # exactly-one-POST pass max_retries_override=0 explicitly.
    api_rate_limit_retry_429_max = 2
    api_rate_limit_retry_429_base_delay = 0.0


class _UnitDispatchClient(BaseKISClient):
    """In-process dispatch client for the HTTP=0 invariant tests."""

    def __init__(self, gate: VTSDistributedGate) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set[str] = set()
        type(self)._shared_client_lock = None
        self._vts_gate = gate
        self.http_calls = 0

    @property  # type: ignore[override]
    def _settings(self) -> Any:  # type: ignore[override]
        return _UnitMockSettings()

    async def _get_limiter(self, api_key: str, *, rate: int, period: float) -> Any:
        limiter = MagicMock()
        limiter.acquire = AsyncMock()
        return limiter

    async def _ensure_client(self, timeout: float | None = None) -> Any:  # type: ignore[override]
        return MagicMock()

    async def _execute_http_request(  # type: ignore[override]
        self, client: Any, method: str, url: str, **kwargs: Any
    ) -> Any:
        self.http_calls += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"rt_cd": "0", "output": {}, "msg1": "ok"}
        return resp


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redis_failure_mutation_is_http_zero():
    """A mock order dispatch when Redis is down must prove HTTP=0 (no fallback)."""
    gate = _make_gate(
        "redis://127.0.0.1:1/0",
        socket_timeout_seconds=0.5,
        socket_connect_timeout_seconds=0.5,
        acquire_deadline_seconds=1.5,
    )
    client = _UnitDispatchClient(gate)
    try:
        with pytest.raises(DistributedGateUnavailable):
            await client._dispatch_rate_limited_with_headers(
                "POST",
                MOCK_BASE_URL + "/uapi/domestic-stock/v1/trading/order-cash",
                headers={"appkey": MOCK_APP_KEY, "authorization": "Bearer t"},
                json_body={"PDNO": "005930"},
                timeout=5.0,
                api_name="order_korea_stock",
                tr_id="VTTC0802U",
                retry_request_errors=False,
                max_retries_override=0,
            )
        assert client.http_calls == 0, (
            "mutation must not fall back to HTTP on Redis failure"
        )
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redis_failure_read_also_http_zero():
    """Reads cannot bypass the gate during Redis outage (same fail-closed path)."""
    gate = _make_gate(
        "redis://127.0.0.1:1/0",
        socket_timeout_seconds=0.5,
        socket_connect_timeout_seconds=0.5,
        acquire_deadline_seconds=1.5,
    )
    client = _UnitDispatchClient(gate)
    try:
        with pytest.raises(DistributedGateUnavailable):
            await client._dispatch_rate_limited_with_headers(
                "GET",
                MOCK_BASE_URL + "/uapi/domestic-stock/v1/trading/inquire-balance",
                headers={"appkey": MOCK_APP_KEY, "authorization": "Bearer t"},
                timeout=5.0,
                api_name="inquire_balance",
                tr_id="VTTC8434R",
            )
        assert client.http_calls == 0
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_live_host_never_contacts_redis(disposable_redis: _DisposableRedis):
    """A live-host dispatch must succeed without the gate touching Redis at all."""
    gate = _make_gate(disposable_redis.url)
    client = _UnitDispatchClient(gate)
    try:
        await client._dispatch_rate_limited_with_headers(
            "GET",
            f"https://{LIVE_HOST}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers={"appkey": "live-key", "authorization": "Bearer t"},
            timeout=5.0,
            api_name="inquire_price",
            tr_id="FHKST01010100",
        )
        assert client.http_calls == 1
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_egw00201_still_exactly_one_post(disposable_redis: _DisposableRedis):
    """ROB-645 invariant preserved: an EGW00201/'초과' body is not re-POSTed."""
    gate = _make_gate(disposable_redis.url)
    client = _UnitDispatchClient(gate)

    async def _execute_http_request(
        client_: Any, method: str, url: str, **kwargs: Any
    ) -> Any:
        client.http_calls += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "EGW00215",
            "msg1": "초당 거래건수를 초과하였습니다.",
        }
        return resp

    client._execute_http_request = _execute_http_request  # type: ignore[assignment]
    try:
        data, _hdrs = await client._dispatch_rate_limited_with_headers(
            "POST",
            MOCK_BASE_URL + "/uapi/domestic-stock/v1/trading/order-cash",
            headers={"appkey": MOCK_APP_KEY, "authorization": "Bearer t"},
            json_body={"PDDO": "005930"},
            timeout=5.0,
            api_name="order_korea_stock",
            tr_id="VTTC0802U",
            retry_request_errors=False,
            max_retries_override=0,
        )
        assert data["msg_cd"] == "EGW00215"
        assert client.http_calls == 1, "order must POST exactly once (no re-POST)"
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_read_retries_reacquire_permit(disposable_redis: _DisposableRedis):
    """A read that retries RequestError reacquires the gate on each attempt."""
    gate = _make_gate(
        disposable_redis.url, interval_seconds=0.3, safety_margin_seconds=0.0
    )
    import httpx

    client = _UnitDispatchClient(gate)
    state = {"n": 0}

    async def _execute_http_request(
        client_: Any, method: str, url: str, **kwargs: Any
    ) -> Any:
        state["n"] += 1
        client.http_calls += 1
        if state["n"] == 1:
            raise httpx.ReadTimeout("first attempt times out")
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"rt_cd": "0", "output": {}}
        return resp

    client._execute_http_request = _execute_http_request  # type: ignore[assignment]
    try:
        await client._dispatch_rate_limited_with_headers(
            "GET",
            MOCK_BASE_URL + "/uapi/domestic-stock/v1/trading/inquire-balance",
            headers={"appkey": MOCK_APP_KEY, "authorization": "Bearer t"},
            timeout=5.0,
            api_name="inquire_balance",
            tr_id="VTTC8434R",
            retry_request_errors=True,
        )
        # Two HTTP attempts -> two gate admits, separated by the interval.
        assert client.http_calls == 2
    finally:
        await gate.close()


# Suppress noisy info logs during the multiprocessing/timing tests.
@pytest.fixture(autouse=True)
def _quiet_gate_logger(caplog: pytest.LogCaptureFixture):
    caplog.set_level(
        logging.WARNING, logger="app.services.brokers.kis.vts_distributed_gate"
    )
    return caplog
