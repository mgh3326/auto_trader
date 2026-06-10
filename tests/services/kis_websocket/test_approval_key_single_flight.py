"""ROB-262: concurrent cold-start single-flight issuance + controlled reissue.

These tests pin the Redis-backed single-flight lock that guards KIS websocket
approval-key issuance so that N simultaneous cold-start callers result in *at
most one* approval-endpoint call and every successful caller observes the same
cached key. They also pin the controlled (lock-guarded) reissue path used by the
OPSP0011 reconnect handler, which must not delete/reissue in a tight loop.

No secret values are exercised — the issued "key" is an opaque test token.
"""

import asyncio

import pytest

from app.services.kis_websocket_internal import approval_keys

pytestmark = pytest.mark.asyncio

INTERNAL = "app.services.kis_websocket_internal.approval_keys"


class _FakeRedis:
    """Minimal async Redis double modelling the operations the lock needs.

    Atomicity is faithful: every method runs to completion without an internal
    ``await``, so ``set(nx=True)`` is genuinely single-winner across interleaved
    coroutines (the only yield points are in the code under test). ``eval`` is
    the compare-and-delete release script.
    """

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, bool]] = []

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        del ex
        self.set_calls.append((key, value, nx))
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.strings:
                del self.strings[key]
                removed += 1
        return removed

    async def eval(self, script: str, key_count: int, key: str, token: str) -> int:
        del script, key_count
        if self.strings.get(key) == token:
            self.strings.pop(key, None)
            return 1
        return 0


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()

    async def _get_client() -> _FakeRedis:
        return redis

    monkeypatch.setattr(approval_keys, "_get_redis_client", _get_client)
    # Keep waits short + deterministic for tests.
    monkeypatch.setattr(approval_keys, "APPROVAL_KEY_WAIT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(approval_keys, "APPROVAL_KEY_WAIT_POLL_SECONDS", 0.01)
    return redis


def _live_cache_key() -> str:
    return approval_keys.APPROVAL_KEY_CACHE_KEYS["kis_live"]


def _live_lock_key() -> str:
    return approval_keys.APPROVAL_KEY_LOCK_CACHE_KEYS["kis_live"]


async def test_cache_hit_returns_without_lock_or_issue(fake_redis, monkeypatch):
    fake_redis.strings[_live_cache_key()] = "warm-key"
    issue_calls = 0

    async def _issue(account_mode: str = "kis_live") -> str:
        nonlocal issue_calls
        issue_calls += 1
        return "should-not-be-called"

    monkeypatch.setattr(approval_keys, "_issue_approval_key", _issue)

    result = await approval_keys.get_approval_key("kis_live")

    assert result == "warm-key"
    assert issue_calls == 0
    # No lock was ever taken on a pure cache hit.
    assert _live_lock_key() not in fake_redis.strings


async def test_concurrent_cold_start_issues_exactly_once(fake_redis, monkeypatch):
    """N simultaneous cold-start callers -> at most one endpoint call, same key."""
    issue_calls = 0

    async def _issue(account_mode: str = "kis_live") -> str:
        nonlocal issue_calls
        issue_calls += 1
        # Simulate the ~10s approval HTTP round-trip yielding control so the
        # other coroutines pile up as lock contenders rather than issuers.
        await asyncio.sleep(0.05)
        return "issued-key"

    monkeypatch.setattr(approval_keys, "_issue_approval_key", _issue)

    results = await asyncio.gather(
        *(approval_keys.get_approval_key("kis_live") for _ in range(10))
    )

    assert issue_calls == 1, f"expected single issuance, got {issue_calls}"
    assert results == ["issued-key"] * 10
    assert fake_redis.strings[_live_cache_key()] == "issued-key"
    # Lock released after issuance.
    assert _live_lock_key() not in fake_redis.strings


async def test_contender_reuses_cached_key_after_wait(fake_redis, monkeypatch):
    """A caller that loses the lock waits, re-reads Redis, and reuses the key."""
    # Pre-acquire the lock with a foreign token so our caller is a contender.
    fake_redis.strings[_live_lock_key()] = "another-owner-token"
    issue_calls = 0

    async def _issue(account_mode: str = "kis_live") -> str:
        nonlocal issue_calls
        issue_calls += 1
        return "must-not-issue"

    monkeypatch.setattr(approval_keys, "_issue_approval_key", _issue)

    async def _holder_populates() -> None:
        await asyncio.sleep(0.02)
        fake_redis.strings[_live_cache_key()] = "holder-key"

    contender = asyncio.create_task(approval_keys.get_approval_key("kis_live"))
    populate = asyncio.create_task(_holder_populates())
    result, _ = await asyncio.gather(contender, populate)

    assert result == "holder-key"
    assert issue_calls == 0  # contender never issues independently


async def test_contender_times_out_and_fails_without_issuing(fake_redis, monkeypatch):
    """If no cached key appears within the bounded wait, fail/backoff (no issue)."""
    fake_redis.strings[_live_lock_key()] = "another-owner-token"
    issue_calls = 0

    async def _issue(account_mode: str = "kis_live") -> str:
        nonlocal issue_calls
        issue_calls += 1
        return "must-not-issue"

    monkeypatch.setattr(approval_keys, "_issue_approval_key", _issue)

    with pytest.raises(approval_keys.ApprovalKeyIssuanceUnavailable):
        await approval_keys.get_approval_key("kis_live")

    assert issue_calls == 0


async def test_lock_holder_failure_releases_lock_and_propagates(
    fake_redis, monkeypatch
):
    """A failed lock holder must release the lock (no deadlock) and surface error."""

    async def _issue(account_mode: str = "kis_live") -> str:
        raise RuntimeError("approval endpoint down")

    monkeypatch.setattr(approval_keys, "_issue_approval_key", _issue)

    with pytest.raises(RuntimeError, match="approval endpoint down"):
        await approval_keys.get_approval_key("kis_live")

    # Lock released despite the failure -> a later caller is not deadlocked.
    assert _live_lock_key() not in fake_redis.strings

    async def _issue_ok(account_mode: str = "kis_live") -> str:
        return "recovered-key"

    monkeypatch.setattr(approval_keys, "_issue_approval_key", _issue_ok)
    assert await approval_keys.get_approval_key("kis_live") == "recovered-key"


async def test_invalidate_and_reissue_forces_fresh_under_lock(fake_redis, monkeypatch):
    """Controlled reissue overwrites a stale cached key with a fresh one (once)."""
    fake_redis.strings[_live_cache_key()] = "stale-key"
    issue_calls = 0

    async def _issue(account_mode: str = "kis_live") -> str:
        nonlocal issue_calls
        issue_calls += 1
        return "fresh-key"

    monkeypatch.setattr(approval_keys, "_issue_approval_key", _issue)

    result = await approval_keys.invalidate_and_reissue_approval_key("kis_live")

    assert result == "fresh-key"
    assert issue_calls == 1  # forced fresh issuance even though a value was cached
    assert fake_redis.strings[_live_cache_key()] == "fresh-key"
    assert _live_lock_key() not in fake_redis.strings


async def test_invalidate_and_reissue_clears_bad_key_when_issue_fails(
    fake_redis, monkeypatch
):
    """If forced reissue fails, the known-bad key is gone (contenders won't reuse)."""
    fake_redis.strings[_live_cache_key()] = "bad-key"

    async def _issue(account_mode: str = "kis_live") -> str:
        raise RuntimeError("reissue failed")

    monkeypatch.setattr(approval_keys, "_issue_approval_key", _issue)

    with pytest.raises(RuntimeError, match="reissue failed"):
        await approval_keys.invalidate_and_reissue_approval_key("kis_live")

    assert _live_cache_key() not in fake_redis.strings
    assert _live_lock_key() not in fake_redis.strings


async def test_mock_and_live_namespaces_are_isolated(fake_redis, monkeypatch):
    """Concurrent live + mock cold starts use disjoint lock + cache namespaces.

    If the lock keys collided, one namespace's holder would block the other and
    only one issuance would happen; asserting BOTH issue (count == 2) proves the
    per-account-mode lock keys are genuinely disjoint, not just the cache keys.
    """
    issue_calls = 0

    async def _issue(account_mode: str = "kis_live") -> str:
        nonlocal issue_calls
        issue_calls += 1
        await asyncio.sleep(0.01)
        return f"{account_mode}-key"

    monkeypatch.setattr(approval_keys, "_issue_approval_key", _issue)
    live, mock = await asyncio.gather(
        approval_keys.get_approval_key("kis_live"),
        approval_keys.get_approval_key("kis_mock"),
    )

    assert live == "kis_live-key"
    assert mock == "kis_mock-key"
    assert issue_calls == 2  # both namespaces issued — locks did not collide
    assert fake_redis.strings[approval_keys.APPROVAL_KEY_CACHE_KEYS["kis_live"]] == (
        "kis_live-key"
    )
    assert fake_redis.strings[approval_keys.APPROVAL_KEY_CACHE_KEYS["kis_mock"]] == (
        "kis_mock-key"
    )


async def test_wait_for_cached_key_reads_before_sleeping(fake_redis, monkeypatch):
    """A contender that finds the key already cached returns with no wasted sleep."""
    fake_redis.strings[_live_cache_key()] = "already-there"
    sleeps = 0
    real_sleep = asyncio.sleep

    async def _counting_sleep(delay, *args, **kwargs):
        nonlocal sleeps
        sleeps += 1
        await real_sleep(0)

    monkeypatch.setattr(approval_keys.asyncio, "sleep", _counting_sleep)

    result = await approval_keys._wait_for_cached_approval_key("kis_live")

    assert result == "already-there"
    assert sleeps == 0  # read-first: no poll sleep when the key is already present


async def test_logs_never_leak_key_secret_or_lock_token(
    fake_redis, monkeypatch, caplog
):
    """AC5 regression guard: cold-start issuance + controlled reissue redact secrets."""
    secrets = {
        "approval-key": "SECRET_APPROVAL_KEY_VALUE",
        "appkey": "SECRET_APP_KEY",
        "secret": "SECRET_APP_SECRET",
        "account": "12345678-01",
    }

    async def _issue(account_mode: str = "kis_live") -> str:
        return secrets["approval-key"]

    monkeypatch.setattr(approval_keys, "_issue_approval_key", _issue)

    with caplog.at_level("DEBUG", logger="app.services.kis_websocket_internal"):
        first = await approval_keys.get_approval_key("kis_live")  # cold-start issue
        await approval_keys.get_approval_key("kis_live")  # cache hit
        await approval_keys.invalidate_and_reissue_approval_key("kis_live")  # reissue

    assert first == secrets["approval-key"]
    # The issued key + every configured secret must be absent from all log output,
    # and so must any opaque lock token (uuid) — logs carry account_mode/booleans only.
    for sensitive in secrets.values():
        assert sensitive not in caplog.text
    lock_token = fake_redis.strings.get(_live_lock_key())  # released -> None
    assert lock_token is None or lock_token not in caplog.text
