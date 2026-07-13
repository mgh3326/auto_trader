"""ROB-843 P1 — pre-send freshness fires at the REAL KIS HTTP send boundary.

These tests drive the actual domestic order service (`order_korea_stock`) and
the real transport (`_request_with_rate_limit` → dispatch loop), monkeypatching
only the lowest-level HTTP dispatch (`_execute_http_request`). They prove the
hook fires immediately before every real POST — after token/limiter/client prep
and on token-refresh re-sends — so a book that goes stale during those awaits
blocks the POST with ZERO HTTP calls. Live callers pass no hook (unchanged).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.brokers.kis.circuit_breaker as cb
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.domestic_orders import DomesticOrderClient
from app.services.brokers.kis.mock_scalping.contract import ReasonCode
from app.services.brokers.kis.pre_send import PreSendFreshnessError

_NXT = "app.services.brokers.kis.domestic_orders.is_nxt_eligible"


class _Settings:
    kis_app_key = "k"
    kis_app_secret = "s"
    kis_access_token = "t"
    kis_account_no = "1234567890"
    api_rate_limit_retry_429_max = 0
    api_rate_limit_retry_429_base_delay = 0.0
    kis_rate_limit_rate = 19
    kis_rate_limit_period = 1.0


class _Parent(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set = set()
        type(self)._shared_client_lock = None
        self._hdr_base = {"content-type": "application/json"}
        tok = MagicMock()
        tok.clear_token = AsyncMock()
        self._token_manager = tok

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return _Settings()

    def _kis_url(self, path: str) -> str:
        return f"https://mockhost{path}"

    async def _ensure_token(self) -> None:
        return None


def _http_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.json = lambda: payload
    return resp


def _make(execute: AsyncMock) -> DomesticOrderClient:
    parent = _Parent()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    parent._get_limiter = AsyncMock(return_value=limiter)  # type: ignore[method-assign]
    parent._ensure_client = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    parent._execute_http_request = execute  # type: ignore[method-assign]
    return DomesticOrderClient(parent)


@pytest.fixture(autouse=True)
def _reset_breaker():
    cb.reset_kis_circuit_breaker()
    yield
    cb.reset_kis_circuit_breaker()


async def _stale_hook() -> None:
    raise PreSendFreshnessError((ReasonCode.STALE_DATA,))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_hook_blocks_before_real_post(monkeypatch) -> None:
    execute = AsyncMock(return_value=_http_response({"rt_cd": "0"}))
    client = _make(execute)
    monkeypatch.setattr(_NXT, AsyncMock(return_value=False))

    with pytest.raises(PreSendFreshnessError):
        await client.order_korea_stock(
            "005930", "buy", 1, 70000, is_mock=True, pre_send_hook=_stale_hook
        )
    assert execute.await_count == 0  # ZERO real HTTP POSTs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fresh_hook_allows_single_post(monkeypatch) -> None:
    execute = AsyncMock(
        return_value=_http_response(
            {"rt_cd": "0", "msg1": "ok", "output": {"ODNO": "1", "ORD_TMD": "0915"}}
        )
    )
    client = _make(execute)
    monkeypatch.setattr(_NXT, AsyncMock(return_value=False))

    async def _fresh() -> None:
        return None

    result = await client.order_korea_stock(
        "005930", "buy", 1, 70000, is_mock=True, pre_send_hook=_fresh
    )
    assert execute.await_count == 1
    assert result["odno"] == "1"
    assert result["rt_cd"] == "0"  # provider metadata preserved (ROB-843)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hook_rechecked_on_token_refresh_resend(monkeypatch) -> None:
    """The token-refresh re-send re-checks freshness before ITS own POST: the
    first POST returns a token-expiry code, then the re-send is blocked (age went
    stale in between), so the second POST never happens."""
    execute = AsyncMock(
        return_value=_http_response(
            {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "token expired"}
        )
    )
    client = _make(execute)
    monkeypatch.setattr(_NXT, AsyncMock(return_value=False))

    calls = {"n": 0}

    async def _fresh_then_stale() -> None:
        calls["n"] += 1
        if calls["n"] >= 2:  # by the retry the book is stale
            raise PreSendFreshnessError((ReasonCode.STALE_DATA,))

    with pytest.raises(PreSendFreshnessError):
        await client.order_korea_stock(
            "005930", "buy", 1, 70000, is_mock=True, pre_send_hook=_fresh_then_stale
        )
    assert execute.await_count == 1  # first POST only; the re-send was blocked
    assert client._parent._token_manager.clear_token.await_count == 1
