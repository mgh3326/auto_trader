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
)
from app.services.brokers.kiwoom.us_client import (
    KiwoomMockUsClient,
    _PerTrRateLimiter,
)


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

    assert clock.sleeps == [0.75]
    assert clock.now == 1.0


@pytest.mark.asyncio
async def test_mock_us_rate_limiter_serializes_concurrent_same_tr() -> None:
    clock = FakeMonotonicClock()
    limiter = _PerTrRateLimiter(clock=clock, sleep=clock.sleep)

    await asyncio.gather(*(limiter.wait("ust21050") for _ in range(3)))

    assert clock.sleeps == [1.0, 1.0]
    assert clock.now == 2.0


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
        ("ust21050", 1.0),
        ("ust21510", 1.0),
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
