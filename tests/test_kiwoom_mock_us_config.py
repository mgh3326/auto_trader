from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings, validate_kiwoom_mock_us_config
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.client import (
    KiwoomConfigurationError,
    KiwoomEndpointError,
    KiwoomMockClient,
    _PerTrRateLimiter,
)
from app.services.brokers.kiwoom.us_client import KiwoomMockUsClient

INTERVAL = constants.TR_MIN_INTERVAL_SECONDS


class FakeMonotonicClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def test_settings_have_kiwoom_mock_us_defaults() -> None:
    fields = Settings.model_fields
    assert fields["kiwoom_mock_us_enabled"].default is False
    assert fields["kiwoom_mock_us_app_key"].default is None
    assert fields["kiwoom_mock_us_app_secret"].default is None
    assert fields["kiwoom_mock_us_account_no"].default is None


@pytest.mark.asyncio
async def test_mock_us_rate_limiter_spaces_same_tr_without_serializing_others() -> None:
    clock = FakeMonotonicClock()
    limiter = _PerTrRateLimiter(clock=clock, sleep=clock.sleep)

    await limiter.wait("ust21050")
    clock.now = 0.25
    await limiter.wait("ust21510")
    await limiter.wait("ust21050")

    assert clock.sleeps == [pytest.approx(INTERVAL - 0.25)]
    assert clock.now == pytest.approx(INTERVAL)


@pytest.mark.asyncio
async def test_mock_us_rate_limiter_serializes_concurrent_same_tr() -> None:
    clock = FakeMonotonicClock()
    limiter = _PerTrRateLimiter(clock=clock, sleep=clock.sleep)

    await asyncio.gather(*(limiter.wait("ust21050") for _ in range(3)))

    assert clock.sleeps == [pytest.approx(INTERVAL)] * 2
    assert clock.now == pytest.approx(2 * INTERVAL)


@pytest.mark.asyncio
async def test_mock_us_client_applies_rate_limit_at_api_dispatch() -> None:
    clock = FakeMonotonicClock()
    dispatch_times: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        dispatch_times.append((request.headers[constants.HEADER_API_ID], clock.now))
        return httpx.Response(200, json={"return_code": 0})

    client = KiwoomMockUsClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="US-AK",
        app_secret="US-SK",
        account_no="US-ACCOUNT",
        rate_limit_clock=clock,
        rate_limit_sleep=clock.sleep,
    )
    client.set_transport_for_test(httpx.MockTransport(handler), token="TKN")

    for api_id in ("ust21050", "ust21050", "ust21510"):
        await client.post_api(api_id=api_id, path="/api/mock", body={})

    assert dispatch_times == [
        ("ust21050", 0.0),
        ("ust21050", pytest.approx(INTERVAL)),
        ("ust21510", pytest.approx(INTERVAL)),
    ]


def _identity(app_key: str) -> str:
    import hashlib

    return hashlib.sha256(app_key.encode("utf-8")).hexdigest()


def _client(app_key: str, handler) -> KiwoomMockClient:
    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key=app_key,
        app_secret="SK",
        account_no="ACCOUNT",
    )
    client.set_transport_for_test(httpx.MockTransport(handler), token="TKN")
    return client


@pytest.mark.asyncio
async def test_pacing_is_shared_by_separately_constructed_clients_per_app_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handlers build a fresh client per tool call; pacing must outlive it."""
    from app.services.brokers.kiwoom import client as client_mod

    clock = FakeMonotonicClock()
    dispatch_times: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        dispatch_times.append((request.headers[constants.HEADER_API_ID], clock.now))
        return httpx.Response(200, json={"return_code": 0})

    for key in ("KEY-A", "KEY-B"):
        monkeypatch.setitem(
            client_mod._SHARED_TR_LIMITERS,
            _identity(key),
            _PerTrRateLimiter(clock=clock, sleep=clock.sleep),
        )

    await _client("KEY-A", handler).post_api(api_id="kt00007", path="/p", body={})
    await _client("KEY-A", handler).post_api(api_id="kt00007", path="/p", body={})
    # A different api-id and a different app key never wait on KEY-A/kt00007.
    await _client("KEY-A", handler).post_api(api_id="kt00009", path="/p", body={})
    await _client("KEY-B", handler).post_api(api_id="kt00007", path="/p", body={})

    assert dispatch_times == [
        ("kt00007", 0.0),
        ("kt00007", pytest.approx(1.2)),
        ("kt00009", pytest.approx(1.2)),
        ("kt00007", pytest.approx(1.2)),
    ]


def test_clients_without_injected_clock_use_the_shared_limiter() -> None:
    first = _client("KEY-C", lambda r: httpx.Response(200, json={"return_code": 0}))
    second = _client("KEY-C", lambda r: httpx.Response(200, json={"return_code": 0}))
    other = _client("KEY-D", lambda r: httpx.Response(200, json={"return_code": 0}))

    assert first._tr_rate_limiter is second._tr_rate_limiter
    assert first._tr_rate_limiter is not other._tr_rate_limiter


def test_tr_interval_is_1_2_seconds() -> None:
    assert INTERVAL == 1.2


@pytest.mark.asyncio
async def test_kr_client_spaces_same_api_id_by_1_2_seconds_not_others() -> None:
    clock = FakeMonotonicClock()
    dispatch_times: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        dispatch_times.append((request.headers[constants.HEADER_API_ID], clock.now))
        return httpx.Response(200, json={"return_code": 0})

    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="KR-AK",
        app_secret="KR-SK",
        account_no="KR-ACCOUNT",
        rate_limit_clock=clock,
        rate_limit_sleep=clock.sleep,
    )
    client.set_transport_for_test(httpx.MockTransport(handler), token="TKN")

    for api_id in ("kt00007", "kt00007", "kt00009", "kt00007"):
        await client.post_api(api_id=api_id, path="/api/mock", body={})

    assert dispatch_times == [
        ("kt00007", 0.0),
        ("kt00007", pytest.approx(1.2)),
        ("kt00009", pytest.approx(1.2)),
        ("kt00007", pytest.approx(2.4)),
    ]


def test_validator_reports_only_us_env_names() -> None:
    obj = SimpleNamespace(
        kiwoom_mock_us_enabled=False,
        kiwoom_mock_us_app_key=None,
        kiwoom_mock_us_app_secret="",
        kiwoom_mock_us_account_no=" ",
        kiwoom_mock_app_key="KR-AK",
        kiwoom_mock_app_secret="KR-SK",
        kiwoom_mock_account_no="KR-ACCOUNT",
    )
    assert validate_kiwoom_mock_us_config(obj) == [
        "KIWOOM_MOCK_US_ENABLED",
        "KIWOOM_MOCK_US_APP_KEY",
        "KIWOOM_MOCK_US_APP_SECRET",
        "KIWOOM_MOCK_US_ACCOUNT_NO",
    ]


def test_us_factory_never_falls_back_to_kr_credentials(monkeypatch) -> None:
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_secret", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_account_no", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_key", "KR-AK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_secret", "KR-SK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_account_no", "KR-ACCOUNT")

    with pytest.raises(KiwoomConfigurationError) as exc:
        KiwoomMockUsClient.from_app_settings()

    message = str(exc.value)
    assert "KIWOOM_MOCK_US_APP_KEY" in message
    assert "KR-AK" not in message
    assert "KR-SK" not in message
    assert "KR-ACCOUNT" not in message


def test_us_factory_builds_distinct_auth_instance(monkeypatch) -> None:
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", "US-AK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_secret", "US-SK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_account_no", "US-ACCOUNT")

    first = KiwoomMockUsClient.from_app_settings()
    second = KiwoomMockUsClient.from_app_settings()

    assert first is not second
    assert first._auth is not second._auth


def test_us_factory_rejects_live_host(monkeypatch) -> None:
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", "US-AK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_secret", "US-SK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_account_no", "US-ACCOUNT")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_base_url", "https://api.kiwoom.com")
    with pytest.raises(KiwoomEndpointError):
        KiwoomMockUsClient.from_app_settings()
