"""G1 wiring: Settings-backed Redis remains a closed pre-dispatch boundary."""

from __future__ import annotations

import logging

import httpx
import pytest

from app.services.brokers.kiwoom import auth as auth_module
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.client import (
    KiwoomConfigurationError,
    KiwoomMockClient,
    KiwoomPreDispatchError,
)
from app.services.brokers.kiwoom.us_client import KiwoomMockUsClient


class _FakeRedis:
    async def get(self, _key: str) -> None:
        return None


class _UnavailableRedis:
    async def get(self, _key: str) -> None:
        raise OSError("redis transport unavailable")


def _configure_distinct_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_key", "KR-APP-KEY")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_secret", "KR-APP-SECRET")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_account_no", "KR-ACCOUNT-001")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", "US-APP-KEY")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_secret", "US-APP-SECRET")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_account_no", "US-ACCOUNT-001")


@pytest.mark.asyncio
async def test_kr_and_us_factories_use_settings_redis_not_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ENV_FILE-loaded Settings path, not REDIS_URL export, owns wiring."""

    from app.core import config as cfg

    _configure_distinct_lanes(monkeypatch)
    monkeypatch.delenv("REDIS_URL", raising=False)
    seen: list[tuple[str, dict[str, object]]] = []

    def from_url(url: str, **kwargs: object) -> _FakeRedis:
        seen.append((url, kwargs))
        return _FakeRedis()

    monkeypatch.setattr(auth_module.redis, "from_url", from_url)
    kr_client = KiwoomMockClient.from_app_settings()
    us_client = KiwoomMockUsClient.from_app_settings()

    assert kr_client._auth._redis_settings is cfg.settings
    assert us_client._auth._redis_settings is cfg.settings
    await kr_client._auth._get_redis()
    await us_client._auth._get_redis()
    assert [url for url, _kwargs in seen] == [
        cfg.settings.get_redis_url(),
        cfg.settings.get_redis_url(),
    ]


@pytest.mark.asyncio
async def test_m1_missing_settings_redis_blocks_before_order_dispatch() -> None:
    """M1: no Settings-backed Redis configuration cannot open an order path."""

    dispatches = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal dispatches
        dispatches += 1
        return httpx.Response(200, json={"return_code": 0})

    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="KR-APP-KEY",
        app_secret="KR-APP-SECRET",
        account_no="KR-ACCOUNT-001",
    )
    client._transport = httpx.MockTransport(handler)

    with pytest.raises(KiwoomPreDispatchError) as exc_info:
        await client.post_api(
            api_id=constants.ORDER_BUY_API_ID,
            path=constants.ORDER_PATH,
            body={},
        )

    assert exc_info.value.stage == "token_resolution"
    assert exc_info.value.cause_type == "KiwoomRedisConfigurationError"
    assert dispatches == 0


@pytest.mark.asyncio
async def test_m2_unreachable_redis_blocks_before_order_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2: a configured but unavailable Redis still fails closed."""

    _configure_distinct_lanes(monkeypatch)
    monkeypatch.setattr(
        auth_module.redis, "from_url", lambda *_a, **_kw: _UnavailableRedis()
    )
    dispatches = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal dispatches
        dispatches += 1
        return httpx.Response(200, json={"return_code": 0})

    client = KiwoomMockClient.from_app_settings()
    client._transport = httpx.MockTransport(handler)

    with pytest.raises(KiwoomPreDispatchError) as exc_info:
        await client.post_api(
            api_id=constants.ORDER_BUY_API_ID,
            path=constants.ORDER_PATH,
            body={},
        )

    assert exc_info.value.stage == "token_resolution"
    assert exc_info.value.cause_type == "OSError"
    assert dispatches == 0


@pytest.mark.parametrize("lane", ["kr", "us"])
def test_m3_cross_lane_identity_equality_is_rejected(
    monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    """M3: exact equality proves either lane cannot be wired to the other."""

    _configure_distinct_lanes(monkeypatch)
    from app.core import config as cfg

    if lane == "kr":
        monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", "KR-APP-KEY")
        factory = KiwoomMockClient.from_app_settings
    else:
        monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_key", "US-APP-KEY")
        factory = KiwoomMockUsClient.from_app_settings

    with pytest.raises(KiwoomConfigurationError, match="identities must be distinct"):
        factory()


@pytest.mark.asyncio
async def test_m4_predispatch_failure_never_logs_or_renders_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """M4: redacted pre-dispatch errors keep both secret and account values out."""

    _configure_distinct_lanes(monkeypatch)
    monkeypatch.setattr(
        auth_module.redis, "from_url", lambda *_a, **_kw: _UnavailableRedis()
    )
    client = KiwoomMockClient.from_app_settings()
    caplog.set_level(logging.DEBUG, logger="app.services.brokers.kiwoom")

    with pytest.raises(KiwoomPreDispatchError) as exc_info:
        await client.post_api(
            api_id=constants.ORDER_BUY_API_ID,
            path=constants.ORDER_PATH,
            body={},
        )

    rendered = "\n".join(
        [str(exc_info.value), *(r.getMessage() for r in caplog.records)]
    )
    for forbidden in (
        "KR-APP-SECRET",
        "US-APP-SECRET",
        "KR-ACCOUNT-001",
        "US-ACCOUNT-001",
    ):
        assert forbidden not in rendered
