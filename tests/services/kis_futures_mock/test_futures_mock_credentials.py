"""hk 6022 — 선물옵션 모의 전용 자격증명·토큰 네임스페이스.

증명할 것:
- 선물 경로는 ``KIS_FUTURES_MOCK_*`` 와 ``kis_futures_mock:*`` 네임스페이스만 쓰고
  주식 모의 키·네임스페이스를 집지 않는다.
- 주식 모의·live 경로는 선물 키·네임스페이스를 집지 않는다.
- 키 누락은 네트워크·토큰 조회 전에 env 이름만 담아 거부한다.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.core.timezone import KST
from app.services.brokers.kis.client import KISClient, _KISSettingsView
from app.services.brokers.kis.futures_mock_credentials import (
    FUTURES_MOCK_CREDENTIAL_ENV_NAMES,
    FuturesMockCredentialsMissing,
    missing_futures_mock_credentials,
)
from app.services.brokers.token_issuance import TokenIssuanceCacheOnlyUnavailable
from app.services.kis_futures_mock import constants as C  # noqa: N812
from app.services.kis_futures_mock.place import (
    FuturesPlaceDeps,
    PlaceRequest,
    cancel_futures_mock_order,
    place_futures_mock_order,
)

ROOT = Path(__file__).resolve().parents[3]
CLIENT_SRC = ROOT / "app/services/brokers/kis/client.py"
VIEW_SRC = ROOT / "app/services/kis_futures_read/view.py"
PLACE_SRC = ROOT / "app/services/kis_futures_mock/place.py"

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.run(coro)


class _FakeRedis:
    """Minimal stateful Redis double for token cache and lock behavior."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    async def execute_command(self, *args: Any) -> int:
        command, _script, key_count, key, lock_value = args
        assert command == "EVAL"
        assert key_count == 1
        if self.values.get(key) != lock_value:
            return 0
        del self.values[key]
        return 1


@pytest.fixture(autouse=True)
def _fresh_token_managers():
    """토큰 매니저는 프로세스 공유다 — 테스트 간 로컬 캐시 오염을 막는다."""
    from app.services.redis_token_manager import _get_kis_mock_token_manager

    _get_kis_mock_token_manager.cache_clear()
    yield
    _get_kis_mock_token_manager.cache_clear()


def _set_all_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kis_app_key", "live-key")
    monkeypatch.setattr(settings, "kis_app_secret", "live-secret")
    monkeypatch.setattr(settings, "kis_mock_app_key", "stock-mock-key")
    monkeypatch.setattr(settings, "kis_mock_app_secret", "stock-mock-secret")
    monkeypatch.setattr(settings, "kis_mock_access_token", "stock-mock-token")
    monkeypatch.setattr(settings, "kis_futures_mock_app_key", "futures-key")
    monkeypatch.setattr(settings, "kis_futures_mock_app_secret", "futures-secret")
    monkeypatch.setattr(settings, "kis_futures_mock_access_token", None)


# --- 자격증명 해석 ------------------------------------------------------------


def test_settings_view_resolves_only_the_futures_keys(monkeypatch):
    _set_all_keys(monkeypatch)

    futures = _KISSettingsView(is_mock=True, futures_mock=True)
    assert futures.kis_app_key == "futures-key"
    assert futures.kis_app_secret == "futures-secret"
    assert futures.account_scope == "kis_futures_mock"
    # 선물 계좌는 호출자가 env 로 넘긴다 — 설정 단에서 주식 계좌로 대체하지 않는다.
    assert futures.kis_account_no is None

    stock = _KISSettingsView(is_mock=True)
    assert stock.kis_app_key == "stock-mock-key"
    assert stock.kis_app_secret == "stock-mock-secret"
    assert stock.account_scope == "kis_mock"

    live = _KISSettingsView(is_mock=False)
    assert live.kis_app_key == "live-key"
    assert live.account_scope == "kis_live"


def test_access_token_slot_is_separate_per_scope(monkeypatch):
    _set_all_keys(monkeypatch)
    futures = _KISSettingsView(is_mock=True, futures_mock=True)
    stock = _KISSettingsView(is_mock=True)

    futures.kis_access_token = "futures-token-1"
    assert settings.kis_futures_mock_access_token == "futures-token-1"
    assert settings.kis_mock_access_token == "stock-mock-token"
    assert futures.kis_access_token == "futures-token-1"
    assert stock.kis_access_token == "stock-mock-token"


def test_missing_credentials_refuse_before_any_construction(monkeypatch):
    monkeypatch.setattr(settings, "kis_futures_mock_app_key", None)
    monkeypatch.setattr(settings, "kis_futures_mock_app_secret", None)
    with pytest.raises(FuturesMockCredentialsMissing) as err:
        KISClient(is_mock=True, futures_mock=True)
    assert err.value.missing == (
        "KIS_FUTURES_MOCK_APP_KEY",
        "KIS_FUTURES_MOCK_APP_SECRET",
    )

    monkeypatch.setattr(settings, "kis_futures_mock_app_key", "futures-key")
    with pytest.raises(FuturesMockCredentialsMissing) as err:
        KISClient(is_mock=True, futures_mock=True)
    assert err.value.missing == ("KIS_FUTURES_MOCK_APP_SECRET",)

    # 공백 문자열도 누락이다.
    monkeypatch.setattr(settings, "kis_futures_mock_app_secret", "   ")
    with pytest.raises(FuturesMockCredentialsMissing):
        KISClient(is_mock=True, futures_mock=True)


def test_futures_mock_flag_requires_mock(monkeypatch):
    _set_all_keys(monkeypatch)
    with pytest.raises(ValueError):
        KISClient(is_mock=False, futures_mock=True)


def test_missing_helper_reports_env_names_only(monkeypatch):
    monkeypatch.setattr(settings, "kis_futures_mock_app_key", None)
    monkeypatch.setattr(settings, "kis_futures_mock_app_secret", "secret-value")
    missing = missing_futures_mock_credentials(settings)
    assert missing == ["KIS_FUTURES_MOCK_APP_KEY"]
    assert "secret-value" not in str(missing)
    exc = FuturesMockCredentialsMissing(missing)
    assert "secret-value" not in str(exc)


# --- 토큰 네임스페이스 ----------------------------------------------------------


def test_token_namespaces_are_disjoint_even_with_same_appkey(monkeypatch):
    """같은 앱키·같은 호스트여도 네임스페이스 접두가 달라 서로를 못 읽는다."""
    monkeypatch.setattr(settings, "kis_mock_app_key", "shared-key")
    monkeypatch.setattr(settings, "kis_futures_mock_app_key", "shared-key")
    monkeypatch.setattr(settings, "kis_futures_mock_app_secret", "futures-secret")
    stock = KISClient(is_mock=True)
    futures = KISClient(is_mock=True, futures_mock=True)

    host = settings.kis_mock_base_url.removeprefix("https://").lower()
    fp = hashlib.sha256(b"shared-key").hexdigest()[:16]
    assert stock._token_manager._token_key == f"kis_mock:{host}:{fp}:access_token"
    assert (
        futures._token_manager._token_key
        == f"kis_futures_mock:{host}:{fp}:access_token"
    )
    assert stock._token_manager._lock_key == f"kis_mock:{host}:{fp}:token:lock"
    assert (
        futures._token_manager._lock_key == f"kis_futures_mock:{host}:{fp}:token:lock"
    )
    assert stock._token_manager is not futures._token_manager
    assert (
        futures._token_manager._lock_wait_timeout_seconds
        == stock._token_manager._lock_wait_timeout_seconds
        == 11.0
    )


@pytest.mark.asyncio
async def test_futures_self_issues_under_gatewayd_mode(monkeypatch):
    """gatewayd provider 목록에 선물 모의는 없다 — 자기 네임스페이스로 자가 발급."""
    _set_all_keys(monkeypatch)
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "gatewayd")
    futures = KISClient(is_mock=True, futures_mock=True)
    assert futures._gatewayd_token_provider() is None
    assert KISClient(is_mock=True)._gatewayd_token_provider() == "kis-mock"
    assert KISClient()._gatewayd_token_provider() == "kis-live"

    called = AsyncMock(side_effect=AssertionError("gatewayd must not be called"))
    monkeypatch.setattr("app.services.brokers.kis.base.ensure_gatewayd_token", called)
    monkeypatch.setattr(
        futures, "_fetch_token", AsyncMock(return_value=("futures-token", 3600))
    )
    futures._token_manager.redis_client = _FakeRedis()  # type: ignore[assignment]

    await futures._ensure_token()

    called.assert_not_awaited()
    assert settings.kis_futures_mock_access_token == "futures-token"
    assert settings.kis_mock_access_token == "stock-mock-token"
    cached = json.loads(
        futures._token_manager.redis_client.values[futures._token_manager._token_key]
    )
    assert cached["access_token"] == "futures-token"


@pytest.mark.asyncio
async def test_futures_cache_only_mode_still_refuses_with_its_label(monkeypatch):
    _set_all_keys(monkeypatch)
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "cache_only")
    futures = KISClient(is_mock=True, futures_mock=True)
    assert futures._token_provider_label() == "kis-futures-mock"
    futures._token_manager.redis_client = _FakeRedis()  # type: ignore[assignment]

    with pytest.raises(TokenIssuanceCacheOnlyUnavailable) as err:
        await futures._ensure_token()
    assert "kis-futures-mock" in str(err.value)


@pytest.mark.asyncio
async def test_futures_cached_token_reads_its_own_namespace(monkeypatch):
    _set_all_keys(monkeypatch)
    futures = KISClient(is_mock=True, futures_mock=True)
    key = futures._token_manager._token_key
    fake = _FakeRedis()
    fake.values[key] = json.dumps(
        {
            "access_token": "cached-futures-token",
            "expires_at": time.time() + 7200,
            "created_at": time.time(),
        }
    )
    # 주식 모의 네임스페이스의 토큰은 무시한다.
    fake.values["kis_mock:access_token"] = json.dumps(
        {"access_token": "stock-token", "expires_at": time.time() + 7200}
    )
    futures._token_manager.redis_client = fake  # type: ignore[assignment]
    await futures._ensure_token()
    assert settings.kis_futures_mock_access_token == "cached-futures-token"
    assert settings.kis_mock_access_token == "stock-mock-token"


# --- 파이프라인 거부 ------------------------------------------------------------


def _boom_client() -> Any:
    raise FuturesMockCredentialsMissing(FUTURES_MOCK_CREDENTIAL_ENV_NAMES)


def _deps_with(**overrides: Any) -> FuturesPlaceDeps:
    return replace(
        FuturesPlaceDeps(
            now=lambda: datetime(2026, 11, 24, 10, 0, tzinfo=KST),
            open_read_view=lambda: None,
            new_order_client=lambda: None,
            account_no=lambda: "12345678",
            recorder=lambda: None,
            signal_recorder=lambda: None,
        ),
        **overrides,
    )


def test_confirm_place_refuses_missing_credentials_before_client(monkeypatch):
    monkeypatch.setenv(C.ENABLED_ENV, "true")
    deps = _deps_with(new_order_client=_boom_client)
    result = run(
        place_futures_mock_order(
            PlaceRequest("A05612", "buy", Decimal("400.00"), 1, strategy="s"),
            confirm=True,
            deps=deps,
        )
    )
    assert result["status"] == "refused"
    assert result["error_code"] == "futures_mock_credentials_missing"
    assert result["detail"] == ",".join(FUTURES_MOCK_CREDENTIAL_ENV_NAMES)
    assert result["dispatch_started"] is False


def test_confirm_place_refuses_missing_credentials_from_read_view(monkeypatch):
    monkeypatch.setenv(C.ENABLED_ENV, "true")

    class _StubOrderClient:
        is_mock_client = True
        base_url = C.MOCK_HOST

        async def close(self) -> None:
            pass

    deps = _deps_with(
        new_order_client=lambda: _StubOrderClient(),
        open_read_view=_boom_client,
    )
    result = run(
        place_futures_mock_order(
            PlaceRequest("A05612", "buy", Decimal("400.00"), 1, strategy="s"),
            confirm=True,
            deps=deps,
        )
    )
    assert result["status"] == "refused"
    assert result["error_code"] == "futures_mock_credentials_missing"
    assert result["dispatch_started"] is False


def test_cancel_confirm_refuses_missing_credentials(monkeypatch):
    monkeypatch.setenv(C.ENABLED_ENV, "true")
    deps = _deps_with(new_order_client=_boom_client)
    result = run(
        cancel_futures_mock_order(broker_order_id="0000012345", confirm=True, deps=deps)
    )
    assert result["status"] == "refused"
    assert result["error_code"] == "futures_mock_credentials_missing"
    assert result["dispatch_started"] is False


def test_preview_cancel_never_builds_an_order_client():
    def _built() -> Any:
        raise AssertionError("preview must not build an order client")

    class _Recorder:
        async def get_original_order(self, **kwargs):
            raise RuntimeError("no db")

    deps = _deps_with(new_order_client=_built, recorder=_Recorder)
    result = run(
        cancel_futures_mock_order(
            broker_order_id="0000012345", confirm=False, deps=deps
        )
    )
    assert result["status"] == "refused"
    assert result["error_code"] == "ledger_unavailable"


# --- 정적 배선 증명 -------------------------------------------------------------


def _imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_view_factory_passes_the_futures_flag():
    text = VIEW_SRC.read_text(encoding="utf-8")
    assert "futures_mock=is_mock" in text


def test_client_binds_each_mock_scope_to_its_own_manager_and_keys():
    text = CLIENT_SRC.read_text(encoding="utf-8")
    assert "get_kis_futures_mock_token_manager" in text
    assert "get_kis_mock_token_manager" in text
    for name in (
        "kis_futures_mock_app_key",
        "kis_futures_mock_app_secret",
        "kis_futures_mock_access_token",
    ):
        assert f"settings.{name}" in text


def test_default_deps_build_the_futures_mock_client():
    text = PLACE_SRC.read_text(encoding="utf-8")
    assert "futures_mock=True" in text


def test_credentials_module_is_a_leaf_with_no_io_imports():
    path = ROOT / "app/services/brokers/kis/futures_mock_credentials.py"
    banned = ("httpx", "requests", "redis", "asyncio", "sqlalchemy", "app.")
    bad = [m for m in _imports_of(path) if m.startswith(banned)]
    assert bad == []


def test_settings_defaults_are_absent():
    """미설정(None 또는 공백)이 기본이다 — 주식 모의 키로 대체하지 않는다."""
    for attr in (
        "kis_futures_mock_app_key",
        "kis_futures_mock_app_secret",
        "kis_futures_mock_access_token",
    ):
        assert not str(getattr(settings, attr) or "").strip()
