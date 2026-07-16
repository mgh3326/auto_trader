"""ROB-892 — distributed Redis gate for KIS mock (VTS) REST dispatch.

Test surface (per the ROB-892 acceptance spec + CodeRabbit/independent review):

* A real disposable ``redis-server`` is launched per test module when the
  binary is available; otherwise the CI job Redis service is tried on an
  isolated DB index. If neither is available the suite FAILS (never silently
  skips) — the real-Redis acceptance is required, not optional.
* A multiprocessing suite runs >=2 independent PIDs/clients concurrently
  issuing KR buy, KR sell, and a mock read through the *real* dispatch
  boundary, synchronized by a shared ``Barrier``. Actual
  ``_execute_http_request`` START timestamps are measured (not ``acquire()``
  returns); adjacent starts for one scope meet the configured interval +
  safety margin, and each intended dispatch fires at most once.
* Same credential across TR/path/market/side/read/order shares one key;
  distinct credentials/hosts isolate.
* Redis unavailable / timeout, acquire-deadline, wait-cancellation, and
  pre-send-freshness failure all produce mutation HTTP=0 (no fallback) AND do
  not leak a HALF_OPEN circuit-breaker probe lease (P1 merge blocker).
* Contention loser reruns freshness; no long gate wait follows the final
  freshness check; the deadline bounds every freshness/claim await.
* Live requests are PROVEN to never contact Redis (the gate's ``_claim`` is
  spied and asserted not awaited).
* Malformed Lua results (wrong arity, admission outside {0,1}, negative /
  zero retry) fail closed.
* TTL / cold start / safety margin / bounded backlog are deterministic, and
  keys/logs carry no raw secret.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import os
import socket
import subprocess
import time
from contextlib import closing
from shutil import which
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.circuit_breaker import KISCircuitBreaker
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

# Small measurement tolerance for server-enforced gaps (covers scheduling +
# monotonic-clock noise). Tight enough to actually guarantee interval + margin.
_TIMING_TOLERANCE = 0.03


# ---------------------------------------------------------------------------
# Real Redis resolution: disposable spawn > CI service > FAIL (never skip)
# ---------------------------------------------------------------------------


def _free_tcp_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _RealRedis:
    """Handle for a real Redis used by this module (spawned or service)."""

    def __init__(self, url: str, *, spawned: bool, port: int | None) -> None:
        self.url = url
        self.spawned = spawned
        self.port = port
        self.proc: subprocess.Popen[str] | None = None

    def start_disposable(self) -> None:
        self.port = _free_tcp_port()
        self.url = f"redis://127.0.0.1:{self.port}/0"
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

    def flush(self) -> None:
        if self.spawned and self.port is not None:
            subprocess.run(
                ["redis-cli", "-h", "127.0.0.1", "-p", str(self.port), "FLUSHDB"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        else:
            subprocess.run(
                ["redis-cli", "-n", "15", "FLUSHDB"],
                capture_output=True,
                timeout=5,
                check=False,
            )


def _redis_ping(url: str) -> bool:
    import redis.asyncio as _redis

    async def _p() -> bool:
        c = _redis.from_url(url, socket_connect_timeout=1.0, socket_timeout=1.0)
        try:
            return bool(await c.ping())
        finally:
            await c.aclose()

    try:
        return asyncio.new_event_loop().run_until_complete(_p())
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def real_redis() -> Any:
    """Resolve a real Redis for this module: disposable spawn > CI service > FAIL.

    Never touches production Redis. If Redis is required but unavailable the
    suite fails loudly instead of skipping (ROB-892 acceptance is mandatory).
    """
    # 1. Best isolation: spawn a disposable redis-server on a random port.
    if which("redis-server"):
        rd = _RealRedis(url="", spawned=True, port=None)
        rd.start_disposable()
        try:
            yield rd
        finally:
            rd.stop()
        return

    # 2. CI job Redis service on an isolated DB index (15) so it never clashes
    #    with conftest's DB 0 consumers.
    service_url = os.environ.get("ROB892_TEST_REDIS_URL", "redis://localhost:6379/15")
    if _redis_ping(service_url):
        yield _RealRedis(url=service_url, spawned=False, port=None)
        return

    # 3. Required but unavailable -> fail (never skip silently).
    pytest.fail(
        "ROB-892 real-Redis acceptance requires a disposable redis-server binary "
        "or a reachable Redis service, but neither was available."
    )


def _make_gate(
    redis_url: str,
    *,
    interval_seconds: float = 1.0,
    safety_margin_seconds: float = 0.05,
    acquire_deadline_seconds: float = 10.0,
    ttl_seconds: int = 120,
    socket_timeout_seconds: float = 2.0,
    socket_connect_timeout_seconds: float = 2.0,
    monotonic=time.monotonic,
) -> VTSDistributedGate:
    return VTSDistributedGate(
        redis_url=redis_url,
        interval_seconds=interval_seconds,
        safety_margin_seconds=safety_margin_seconds,
        acquire_deadline_seconds=acquire_deadline_seconds,
        ttl_seconds=ttl_seconds,
        socket_timeout_seconds=socket_timeout_seconds,
        socket_connect_timeout_seconds=socket_connect_timeout_seconds,
        monotonic=monotonic,
    )


# ===========================================================================
# Section 1 — gate module unit/integration tests (real disposable Redis)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_first_admit_is_immediate(real_redis: _RealRedis):
    gate = _make_gate(real_redis.url)
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
async def test_adjacent_admits_meet_interval_plus_margin(real_redis: _RealRedis):
    """Two sequential admits must be >= interval + safety_margin apart."""
    interval, margin = 0.40, 0.05
    gate = _make_gate(
        real_redis.url, interval_seconds=interval, safety_margin_seconds=margin
    )
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="adjacent")
        await gate.acquire(scope, call_class="order")
        t0 = time.monotonic()
        await gate.acquire(scope, call_class="order")
        gap = time.monotonic() - t0
        assert gap >= interval + margin - _TIMING_TOLERANCE, (
            f"second admit came too early: {gap:.3f}s < {interval + margin}s"
        )
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_admits_serialize_no_burst(real_redis: _RealRedis):
    """Three concurrent acquires for the same scope never burst past the budget."""
    interval, margin = 0.30, 0.05
    gate = _make_gate(
        real_redis.url, interval_seconds=interval, safety_margin_seconds=margin
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
        for g in gaps:
            assert g >= interval + margin - _TIMING_TOLERANCE, (
                f"burst detected, gap={g:.3f}s"
            )
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_distinct_credentials_isolate(real_redis: _RealRedis):
    gate = _make_gate(real_redis.url, interval_seconds=0.30, safety_margin_seconds=0.05)
    try:
        scope_a = build_vts_gate_scope_key(host=MOCK_HOST, app_key="cred-A")
        scope_b = build_vts_gate_scope_key(host=MOCK_HOST, app_key="cred-B")
        t0 = time.monotonic()
        await asyncio.gather(
            gate.acquire(scope_a, call_class="order"),
            gate.acquire(scope_b, call_class="order"),
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 0.30, f"distinct scopes did not parallelize: {elapsed:.3f}s"
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_distinct_hosts_isolate(real_redis: _RealRedis):
    gate = _make_gate(real_redis.url, interval_seconds=0.30)
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
async def test_acquire_deadline_raises_fail_closed(real_redis: _RealRedis):
    gate = _make_gate(
        real_redis.url,
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
async def test_cancellation_while_waiting_is_http_zero(real_redis: _RealRedis):
    gate = _make_gate(real_redis.url, interval_seconds=1.0, safety_margin_seconds=0.0)
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="cancel")
        await gate.acquire(scope, call_class="order")  # occupy

        admitted = {"v": False}

        async def _waiter() -> None:
            await gate.acquire(scope, call_class="order")
            admitted["v"] = True

        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admitted["v"] is False
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_freshness_hook_rerun_on_contention(real_redis: _RealRedis):
    gate = _make_gate(real_redis.url, interval_seconds=0.40, safety_margin_seconds=0.0)
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="fresh")
        await gate.acquire(scope, call_class="order")  # occupy slot

        calls = {"n": 0}

        async def _hook() -> None:
            calls["n"] += 1

        await gate.acquire(scope, freshness_hook=_hook, call_class="order")
        assert calls["n"] >= 2, f"freshness not rerun on contention: {calls['n']}"
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_freshness_failure_aborts_before_admit(real_redis: _RealRedis):
    class _FreshError(RuntimeError):
        pass

    gate = _make_gate(real_redis.url)
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="freshfail")
        admitted = {"v": False}

        async def _failing_hook() -> None:
            raise _FreshError("stale book")

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


@pytest.mark.unit
def test_scope_key_is_secret_free():
    """Keys and fingerprints must never contain the raw app key."""
    key = build_vts_gate_scope_key(host=MOCK_HOST, app_key="SUPER_SECRET_VALUE_123")
    assert "SUPER_SECRET_VALUE_123" not in key
    assert key.startswith("kis_mock:gate:openapivts.koreainvestment.com:29443:")
    fp = key.rsplit(":", 1)[-1]
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


@pytest.mark.unit
def test_netloc_helper_matches_token_namespace():
    assert netloc_from_url(MOCK_BASE_URL + "/uapi/x") == MOCK_HOST
    assert netloc_from_url("https://openapi.koreainvestment.com:9443/p") == LIVE_HOST


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cold_start_after_ttl_expiry_is_deterministic(real_redis: _RealRedis):
    gate = _make_gate(
        real_redis.url,
        interval_seconds=0.20,
        safety_margin_seconds=0.0,
        ttl_seconds=1,
    )
    try:
        scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="ttl")
        await gate.acquire(scope, call_class="order")
        await asyncio.sleep(1.2)
        t0 = time.monotonic()
        await gate.acquire(scope, call_class="order")
        assert time.monotonic() - t0 < 0.2  # cold-start immediate admit
    finally:
        await gate.close()


# ===========================================================================
# Section 2 — CR2: strict Lua decoder (malformed results fail closed)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_malformed_lua_results_fail_closed(real_redis: _RealRedis):
    """Every result outside the exact [admission∈{0,1}, retry>=0] protocol fails."""
    gate = _make_gate(real_redis.url)
    scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="malformed")

    bad_results = [
        [2, 0],  # admission outside {0,1} must not authorize HTTP
        [0, -5],  # negative retry rejected (no clamping)
        [0, 0],  # denial with zero retry would busy-loop -> fail closed
        [1, 0, 9],  # wrong arity
        "garbage",  # not a list
        [1],  # too few elements
    ]
    try:
        for raw in bad_results:
            # Inject the raw value at the Redis boundary so the DECODER (which
            # lives inside _claim) processes it — mocking _claim itself would
            # bypass the code under test.
            fake_client = MagicMock()
            fake_client.execute_command = AsyncMock(return_value=raw)
            gate._get_redis = AsyncMock(return_value=fake_client)  # type: ignore[method-assign]
            with pytest.raises(DistributedGateUnavailable):
                await gate.acquire(scope, call_class="order")
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_valid_lua_result_admits(real_redis: _RealRedis):
    gate = _make_gate(real_redis.url)
    scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="valid")
    try:
        fake_client = MagicMock()
        fake_client.execute_command = AsyncMock(return_value=[1, 0])
        gate._get_redis = AsyncMock(return_value=fake_client)  # type: ignore[method-assign]
        r = await gate.acquire(scope, call_class="order")
        assert r.contention_attempts == 1
    finally:
        await gate.close()


# ===========================================================================
# Section 3 — CR3: deadline enforcement (precheck, wait_for bounds, recheck)
# ===========================================================================


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, d: float) -> None:
        self.t += d


@pytest.mark.asyncio
@pytest.mark.integration
async def test_expired_deadline_raises_before_any_claim():
    """An already-expired custom deadline raises immediately; _claim never runs."""
    clock = _FakeClock(1000.0)
    gate = _make_gate("redis://127.0.0.1:1/0", monotonic=clock)
    scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="expired")
    claim = AsyncMock(return_value=[1, 0])
    gate._claim = claim  # type: ignore[method-assign]
    try:
        with pytest.raises(DistributedGateUnavailable, match="already exceeded"):
            await gate.acquire(scope, deadline_monotonic=999.0, call_class="order")
        claim.assert_not_awaited()
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_freshness_crossing_deadline_raises(real_redis: _RealRedis):
    """A freshness hook that runs past the deadline fails closed (claim unused)."""
    gate = _make_gate(
        real_redis.url, interval_seconds=0.20, acquire_deadline_seconds=0.15
    )
    scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="freshcross")
    claim = AsyncMock(return_value=[1, 0])
    gate._claim = claim  # type: ignore[method-assign]

    async def _slow_hook() -> None:
        await asyncio.sleep(0.5)  # >> 0.15 deadline

    try:
        with pytest.raises(DistributedGateUnavailable, match="freshness"):
            await gate.acquire(scope, freshness_hook=_slow_hook, call_class="order")
        claim.assert_not_awaited()
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_claim_crossing_deadline_raises():
    """A claim that exceeds the remaining deadline fails closed."""
    clock = _FakeClock(1000.0)
    gate = _make_gate(
        "redis://127.0.0.1:1/0", monotonic=clock, acquire_deadline_seconds=0.10
    )
    scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="claimcross")

    async def _slow_claim(_scope: str) -> tuple[bool, float]:
        await asyncio.sleep(0.5)  # >> 0.10 budget
        return True, 0.0

    gate._claim = _slow_claim  # type: ignore[method-assign]
    try:
        with pytest.raises(DistributedGateUnavailable, match="claim exceeded"):
            await gate.acquire(scope, call_class="order")
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_admit_after_deadline_consumes_slot_and_raises():
    """A claim that succeeds after the deadline crossed consumes the slot (HTTP=0)."""
    clock = _FakeClock(1000.0)
    gate = _make_gate(
        "redis://127.0.0.1:1/0", monotonic=clock, acquire_deadline_seconds=10.0
    )
    scope = build_vts_gate_scope_key(host=MOCK_HOST, app_key="admitcross")

    async def _admit_then_cross(_scope: str) -> tuple[bool, float]:
        # Claim succeeds, then pushes the injected clock past the deadline.
        clock.advance(20.0)
        return True, 0.0

    gate._claim = _admit_then_cross  # type: ignore[method-assign]
    try:
        with pytest.raises(DistributedGateUnavailable, match="admitted after"):
            await gate.acquire(scope, call_class="order")
    finally:
        await gate.close()


# ===========================================================================
# Section 4 — singleton wiring
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
# Section 5 — real multiprocessing dispatch-timing suite (with Barrier)
# ===========================================================================


class _MockSettings:
    kis_app_key = MOCK_APP_KEY
    kis_app_secret = "secret"
    kis_access_token = "token"
    kis_account_no = "1234567890"
    kis_base_url = MOCK_BASE_URL
    kis_mock_base_url = MOCK_BASE_URL
    api_rate_limit_retry_429_max = 0
    api_rate_limit_retry_429_base_delay = 0.0
    kis_rate_limit_rate = 19
    kis_rate_limit_period = 1.0
    kis_api_rate_limits: dict[str, Any] = {}


class _DispatchTrackingClient(BaseKISClient):
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
        self._dispatch_starts.append(time.monotonic())
        self._dispatch_count += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"rt_cd": "0", "output": {}, "msg1": "ok"}
        return resp


def _child_worker(  # noqa: C901
    redis_url: str,
    app_key: str,
    requests: list[tuple[str, str]],
    out_queue: mp.Queue,
    interval_seconds: float,
    safety_margin_seconds: float,
    barrier: mp.Barrier | None,
) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    gate = _make_gate(
        redis_url,
        interval_seconds=interval_seconds,
        safety_margin_seconds=safety_margin_seconds,
        acquire_deadline_seconds=15.0,
    )
    _MockSettings.kis_app_key = app_key
    _MockSettings.kis_mock_base_url = MOCK_BASE_URL
    client = _DispatchTrackingClient(gate)

    async def _run() -> list[tuple[str, float]]:
        if barrier is not None:
            barrier.wait()
        starts: list[tuple[str, float]] = []
        for label, path in requests:
            await client._dispatch_rate_limited_with_headers(
                "POST" if "order" in label else "GET",
                MOCK_BASE_URL + path,
                headers={
                    "appkey": app_key,
                    "authorization": "Bearer tok",
                    "tr_id": label,
                },
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
        out_queue.put((app_key, starts, client._dispatch_count, None))
    except Exception as e:  # noqa: BLE001
        out_queue.put((app_key, [], -1, repr(e)))
    finally:
        loop.run_until_complete(gate.close())
        loop.close()


def _run_children_and_collect(
    redis_url: str,
    jobs: list[tuple[str, list[tuple[str, str]]]],
    interval: float,
    margin: float,
    synchronize: bool,
) -> list[tuple[str, list[tuple[str, float]], int, str | None]]:
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    n = len(jobs)
    barrier = ctx.Barrier(n) if synchronize and n >= 2 else None
    procs = [
        ctx.Process(
            target=_child_worker,
            args=(redis_url, app_key, reqs, q, interval, margin, barrier),
        )
        for app_key, reqs in jobs
    ]
    for p in procs:
        p.start()
    try:
        for p in procs:
            p.join(timeout=60)
    finally:
        # CR5: reap any timed-out child so a hang never leaks; assert clean exit.
        for p in procs:
            if p.is_alive():  # pragma: no cover — reaping safety
                p.terminate()
                p.join(timeout=5)
                if p.is_alive():
                    p.kill()
                    p.join(timeout=5)
            assert p.exitcode == 0, f"child exited with {p.exitcode}"
    # CR5: collect exactly one result per child with a timeout (no Queue.empty()).
    results = []
    for _ in range(n):
        try:
            results.append(q.get(timeout=10))
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"child produced no result: {e!r}") from e
    return results


@pytest.mark.integration
def test_multiprocessing_kr_buy_sell_read_share_budget(real_redis: _RealRedis):
    """Two PIDs (KR buy + KR sell & mock read) share ONE admit budget. A Barrier
    synchronizes simultaneous departure so real cross-PID contention occurs;
    adjacent _execute_http_request starts meet interval + margin and each
    intended dispatch fires exactly once."""
    real_redis.flush()
    interval, margin = 0.35, 0.05
    reqs_a = [("kr_buy", "/uapi/domestic-stock/v1/trading/order-cash")]
    reqs_b = [
        ("kr_sell", "/uapi/domestic-stock/v1/trading/order-cash"),
        ("mock_read", "/uapi/domestic-stock/v1/trading/inquire-balance"),
    ]
    jobs = [(MOCK_APP_KEY, reqs_a), (MOCK_APP_KEY, reqs_b)]
    results = _run_children_and_collect(
        real_redis.url, jobs, interval, margin, synchronize=True
    )

    all_starts: list[float] = []
    total = 0
    for r in results:
        assert len(r) >= 4 and r[3] is None, f"child reported failure: {r}"
        _app_key, starts, count, _err = r
        assert count >= 1 and count == len(starts)
        total += count
        all_starts.extend(ts for _label, ts in starts)

    assert total == 3, f"expected 3 dispatches, got {total}"
    all_starts.sort()
    gaps = [all_starts[i] - all_starts[i - 1] for i in range(1, len(all_starts))]
    # CR10: adjacent starts must meet interval + margin (only small tolerance),
    # not just the bare interval.
    for g in gaps:
        assert g >= interval + margin - 0.05, (
            f"cross-PID burst: gap {g:.3f}s < {interval + margin}s"
        )


@pytest.mark.integration
def test_multiprocessing_distinct_credentials_do_not_serialize(real_redis: _RealRedis):
    """Two PIDs with DIFFERENT mock app keys admit in parallel (barrier-proven)."""
    real_redis.flush()
    interval = 0.40
    reqs = [("kr_buy", "/uapi/domestic-stock/v1/trading/order-cash")]
    jobs = [("cred-alpha", reqs), ("cred-beta", reqs)]
    results = _run_children_and_collect(
        real_redis.url, jobs, interval, 0.05, synchronize=True
    )

    starts = sorted(ts for r in results for _label, ts in r[1])
    assert len(starts) == 2
    delta = starts[1] - starts[0]
    # CR6/CR11: barrier-synchronized departure + independent scopes -> dispatch
    # starts must be close (parallel admission), well under the interval.
    assert delta < interval * 0.4, (
        f"distinct credentials serialized (starts {delta:.3f}s apart): {results}"
    )
    for r in results:
        assert r[2] == 1, f"each PID must dispatch exactly once: {r}"


# ===========================================================================
# Section 6 — dispatch-boundary integration (HTTP=0 invariants)
# ===========================================================================


class _UnitMockSettings(_MockSettings):
    api_rate_limit_retry_429_max = 2
    api_rate_limit_retry_429_base_delay = 0.0


class _UnitDispatchClient(BaseKISClient):
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
async def test_live_host_never_contacts_redis(real_redis: _RealRedis):
    """CR7: prove Redis is never contacted for a live-host dispatch by spying on
    the gate's _claim (a healthy Redis would otherwise mask a regression)."""
    gate = _make_gate(real_redis.url)
    claim_spy = AsyncMock(wraps=gate._claim)
    gate._claim = claim_spy  # type: ignore[method-assign]
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
        claim_spy.assert_not_awaited()  # live -> zero Redis calls, proven
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mock_dispatch_empty_appkey_fail_closed(real_redis: _RealRedis):
    """CR9: a mock-host dispatch with no appkey must fail closed, not bypass."""
    gate = _make_gate(real_redis.url)
    claim_spy = AsyncMock(wraps=gate._claim)
    gate._claim = claim_spy  # type: ignore[method-assign]
    client = _UnitDispatchClient(gate)
    try:
        with pytest.raises(DistributedGateUnavailable, match="appkey"):
            await client._dispatch_rate_limited_with_headers(
                "POST",
                MOCK_BASE_URL + "/uapi/domestic-stock/v1/trading/order-cash",
                headers={"authorization": "Bearer t"},  # no appkey
                json_body={"PDNO": "005930"},
                timeout=5.0,
                api_name="order_korea_stock",
                tr_id="VTTC0802U",
                retry_request_errors=False,
                max_retries_override=0,
            )
        assert client.http_calls == 0
        claim_spy.assert_not_awaited()
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_egw00201_still_exactly_one_post(real_redis: _RealRedis):
    gate = _make_gate(real_redis.url)
    client = _UnitDispatchClient(gate)

    async def _execute_http_request(  # type: ignore[override]
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
        assert client.http_calls == 1
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_read_retries_reacquire_permit_respecting_interval(
    real_redis: _RealRedis,
):
    """CR8: a read retry must REACQUIRE a permit, and the two admissions must be
    separated by the configured interval (recorded dispatch starts)."""
    interval, margin = 0.30, 0.0
    gate = _make_gate(
        real_redis.url, interval_seconds=interval, safety_margin_seconds=margin
    )
    import httpx

    client = _UnitDispatchClient(gate)
    state = {"n": 0}
    starts: list[float] = []

    async def _execute_http_request(  # type: ignore[override]
        client_: Any, method: str, url: str, **kwargs: Any
    ) -> Any:
        state["n"] += 1
        client.http_calls += 1
        starts.append(time.monotonic())
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
        assert client.http_calls == 2  # retry happened
        assert len(starts) == 2
        gap = starts[1] - starts[0]
        assert gap >= interval + margin - _TIMING_TOLERANCE, (
            f"retry did not respect the interval: gap={gap:.3f}s"
        )
    finally:
        await gate.close()


# ===========================================================================
# Section 7 — P1: HALF_OPEN breaker probe lease released on pre-dispatch abort
# ===========================================================================


class _BreakerSettings:
    kis_circuit_breaker_enabled = True
    kis_circuit_breaker_failure_threshold = 2
    kis_circuit_breaker_cooldown_seconds = 10


def _breaker_open_past_cooldown() -> tuple[KISCircuitBreaker, _FakeClock]:
    """A breaker that is OPEN with the cooldown elapsed.

    The NEXT ``before_request`` (made by the dispatch wrapper) transitions it to
    HALF_OPEN and hands out the probe lease — mirroring the real flow. The test
    must NOT pre-acquire the probe, or the wrapper's own ``before_request`` hits
    the HALF_OPEN stampede guard and raises ``KISCircuitOpen``.
    """
    clock = _FakeClock(1000.0)
    cb = KISCircuitBreaker(now=clock, settings_obj=_BreakerSettings())
    cb.record_failure()
    cb.record_failure()  # threshold reached -> OPEN
    clock.advance(11.0)  # past cooldown -> next before_request() half-opens
    assert cb.state == "open"
    return cb, clock


@pytest.mark.asyncio
@pytest.mark.unit
async def test_half_open_probe_released_on_distributed_gate_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    """P1: a Redis gate failure during a HALF_OPEN probe releases the probe lease
    (next request can probe) WITHOUT changing breaker state/failures."""
    cb, _clock = _breaker_open_past_cooldown()
    monkeypatch.setattr(
        "app.services.brokers.kis.base.get_kis_circuit_breaker", lambda: cb
    )

    gate = _make_gate(
        "redis://127.0.0.1:1/0",
        socket_timeout_seconds=0.3,
        socket_connect_timeout_seconds=0.3,
        acquire_deadline_seconds=0.8,
    )
    client = _UnitDispatchClient(gate)
    try:
        with pytest.raises(DistributedGateUnavailable):
            await client._request_with_rate_limit_with_headers(
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
        assert client.http_calls == 0  # HTTP=0
        assert cb.state == "half_open"  # state unchanged
        assert cb._probe_in_flight is False  # lease released
        assert cb.failure_count == 2  # failures unchanged (still threshold)
        # Next request can obtain a fresh probe (no permanent KISCircuitOpen).
        cb.before_request()
        assert cb._probe_in_flight is True
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_half_open_probe_released_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
):
    """P1: a cancellation during the gate wait releases the probe lease and does
    NOT record a fake KIS reachability outcome (HTTP=0 contract)."""
    cb, _clock = _breaker_open_past_cooldown()
    monkeypatch.setattr(
        "app.services.brokers.kis.base.get_kis_circuit_breaker", lambda: cb
    )

    gate = _make_gate(real_redis_url_unused(), acquire_deadline_seconds=5.0)  # type: ignore[arg-type]

    async def _hanging_claim(_scope: str) -> tuple[bool, float]:
        await asyncio.sleep(5.0)  # hang so the caller is cancelled mid gate-wait
        return False, 1.0

    gate._claim = _hanging_claim  # type: ignore[method-assign]
    client = _UnitDispatchClient(gate)
    try:
        task = asyncio.create_task(
            client._request_with_rate_limit_with_headers(
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
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.http_calls == 0  # HTTP=0
        assert cb.state == "half_open"  # NOT closed/moved by the cancellation
        assert cb._probe_in_flight is False  # lease released
        assert cb.failure_count == 2
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_half_open_probe_released_on_pre_send_freshness(
    monkeypatch: pytest.MonkeyPatch,
):
    """P1: a PreSendFreshnessError (ROB-843) during a HALF_OPEN probe releases the
    lease without moving the breaker (pre-existing ROB-843 path, now hardened)."""
    from app.services.brokers.kis.pre_send import PreSendFreshnessError

    cb, _clock = _breaker_open_past_cooldown()
    monkeypatch.setattr(
        "app.services.brokers.kis.base.get_kis_circuit_breaker", lambda: cb
    )

    gate = _make_gate(real_redis_url_unused())  # type: ignore[arg-type]
    client = _UnitDispatchClient(gate)

    async def _stale_hook() -> None:
        raise PreSendFreshnessError(("stale",))

    try:
        with pytest.raises(PreSendFreshnessError):
            await client._request_with_rate_limit_with_headers(
                "GET",
                f"https://{LIVE_HOST}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers={"appkey": "live-key", "authorization": "Bearer t"},
                timeout=5.0,
                api_name="inquire_price",
                tr_id="FHKST01010100",
                pre_send_hook=_stale_hook,
            )
        assert client.http_calls == 0
        assert cb.state == "half_open"
        assert cb._probe_in_flight is False
        assert cb.failure_count == 2
    finally:
        await gate.close()


def real_redis_url_unused() -> str:
    """A placeholder URL for breaker tests whose gate is never contacted."""
    return "redis://127.0.0.1:1/0"


@pytest.fixture(autouse=True)
def _quiet_gate_logger(caplog: pytest.LogCaptureFixture):
    caplog.set_level(
        logging.WARNING, logger="app.services.brokers.kis.vts_distributed_gate"
    )
    return caplog
