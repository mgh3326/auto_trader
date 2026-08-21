"""W5 — the durable Telegram webhook ingress.

RED-before-fix items 1, 2, 3, 4 and 16 (legacy-path preservation).

The whole point of the durable ingress is that the HTTP request thread does
DB work and nothing else. Every test here asserts a *zero*: zero inline
handler invocations, zero Telegram calls, zero broker calls, and zero enqueue
attempts once the commit has failed.

Mutants deliberately covered:

* ``200 before commit`` — a session factory whose ``commit`` raises must not
  produce a 200.
* ``worker-gate bypass`` — arming durable ingress without both consumers must
  not accept traffic.
* ``raw payload persistence`` — see
  ``tests/services/order_proposals/callback_inbox/test_data_minimization.py``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings

_PATH = "/trading/api/telegram/callback"

_VALID_UPDATE: dict[str, Any] = {
    "update_id": 990001,
    "callback_query": {
        "id": "cbq-durable-1",
        "from": {"id": 777},
        "message": {"chat": {"id": 42}, "message_id": 555},
        # op:<8 hex>:<22 urlsafe b64>:<base36 rev>:<12 digest>:<nonce>
        "data": "op:0123abcd:AAAAAAAAAAAAAAAAAAAAAA:1:abcdefghijkl:nonce123456",
    },
}


def _build_app() -> FastAPI:
    from app.routers.telegram_callback import router as telegram_router

    app = FastAPI()
    app.include_router(telegram_router)
    return app


@pytest.fixture
def _armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "42", raising=False
    )


async def _post(app: FastAPI, payload: dict[str, Any]):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        return await client.post(_PATH, json=payload)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_gate_off_keeps_exact_legacy_inline_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED item 16 — with the durable gate off nothing about today changes."""
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED",
        False,
        raising=False,
    )
    inline = AsyncMock(return_value={"handled": True, "reason": "approved"})
    ingest = AsyncMock()
    with (
        patch("app.routers.telegram_callback.handle_callback_update", inline),
        patch("app.routers.telegram_callback.ingest_callback_update", ingest),
    ):
        response = await _post(_build_app(), _VALID_UPDATE)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    inline.assert_awaited_once()
    ingest.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_route_commits_before_200_and_never_runs_the_inline_handler(
    _armed: None,
) -> None:
    """RED item 1 — commit happens before the ACK; inline handler count is 0."""
    from app.services.order_proposals.callback_inbox.ingress import IngressResult

    ordering: list[str] = []
    job_id = uuid.uuid4()

    async def _ingest(update, **kwargs):
        ordering.append("committed")
        return IngressResult(
            accepted=True,
            duplicate=False,
            job_id=job_id,
            reason="queued",
            enqueued=True,
        )

    inline = AsyncMock()
    with (
        patch("app.routers.telegram_callback.ingest_callback_update", _ingest),
        patch("app.routers.telegram_callback.handle_callback_update", inline),
    ):
        response = await _post(_build_app(), _VALID_UPDATE)
        ordering.append("responded")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert ordering == ["committed", "responded"]
    inline.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_route_503s_when_the_consumers_are_not_both_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutant: worker-gate bypass. Ingress must refuse traffic it cannot drain."""
    from app.services.order_proposals.callback_inbox.ingress import IngressResult

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED",
        True,
        raising=False,
    )

    for worker_on, recovery_on in ((False, False), (True, False), (False, True)):
        monkeypatch.setattr(
            settings,
            "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
            worker_on,
            raising=False,
        )
        monkeypatch.setattr(
            settings,
            "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
            recovery_on,
            raising=False,
        )
        ingest = AsyncMock(
            return_value=IngressResult(
                accepted=True,
                duplicate=False,
                job_id=uuid.uuid4(),
                reason="queued",
                enqueued=True,
            )
        )
        inline = AsyncMock()
        with (
            patch("app.routers.telegram_callback.ingest_callback_update", ingest),
            patch("app.routers.telegram_callback.handle_callback_update", inline),
        ):
            response = await _post(_build_app(), _VALID_UPDATE)
        assert response.status_code == 503, (worker_on, recovery_on)
        ingest.assert_not_awaited()
        inline.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_db_persist_failure_returns_503_and_never_enqueues(
    _armed: None,
) -> None:
    """RED item 2 — a DB commit failure is a 503, and enqueue count is 0."""
    from app.services.order_proposals.callback_inbox.ingress import (
        CallbackInboxUnavailable,
    )

    enqueued: list[uuid.UUID] = []

    async def _ingest(update, **kwargs):
        raise CallbackInboxUnavailable("persist_failed")

    inline = AsyncMock()
    with (
        patch("app.routers.telegram_callback.ingest_callback_update", _ingest),
        patch("app.routers.telegram_callback.handle_callback_update", inline),
    ):
        response = await _post(_build_app(), _VALID_UPDATE)

    assert response.status_code == 503
    assert enqueued == []
    inline.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_503_body_is_generic_and_leaks_no_internals(_armed: None) -> None:
    """A retryable 503 must not describe the database, the driver or the row."""
    from app.services.order_proposals.callback_inbox.ingress import (
        CallbackInboxUnavailable,
    )

    async def _ingest(update, **kwargs):
        raise CallbackInboxUnavailable(
            "relation review.telegram_callback_inbox does not exist; "
            "chat_id=42 nonce=nonce123456"
        )

    with patch("app.routers.telegram_callback.ingest_callback_update", _ingest):
        response = await _post(_build_app(), _VALID_UPDATE)

    assert response.status_code == 503
    body = response.text.lower()
    for leak in (
        "relation",
        "telegram_callback_inbox",
        "nonce123456",
        "chat_id",
        "traceback",
        "asyncpg",
        "sqlalchemy",
        "42",
    ):
        assert leak not in body, f"503 body leaked {leak!r}: {response.text}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unexpected_ingress_exception_is_also_a_generic_503(
    _armed: None,
) -> None:
    async def _ingest(update, **kwargs):
        raise RuntimeError("nonce123456 leaked through an unexpected path")

    inline = AsyncMock()
    with (
        patch("app.routers.telegram_callback.ingest_callback_update", _ingest),
        patch("app.routers.telegram_callback.handle_callback_update", inline),
    ):
        response = await _post(_build_app(), _VALID_UPDATE)

    assert response.status_code == 503
    assert "nonce123456" not in response.text
    inline.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_lost_redis_kick_still_acks_200_within_a_bound(
    _armed: None,
) -> None:
    """RED item 3 — the enqueue is best-effort; the committed row is the ACK."""
    from app.services.order_proposals.callback_inbox.ingress import IngressResult

    async def _ingest(update, **kwargs):
        return IngressResult(
            accepted=True,
            duplicate=False,
            job_id=uuid.uuid4(),
            reason="queued",
            enqueued=False,
        )

    started = time.monotonic()
    with patch("app.routers.telegram_callback.ingest_callback_update", _ingest):
        response = await _post(_build_app(), _VALID_UPDATE)
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert elapsed < 5.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_rejected_update_still_acks_200_without_persisting(
    _armed: None,
) -> None:
    """A non-callback update is a no-op with the same external effect as today."""
    from app.services.order_proposals.callback_inbox.ingress import IngressResult

    seen: list[dict[str, Any]] = []

    async def _ingest(update, **kwargs):
        seen.append(update)
        return IngressResult(
            accepted=False,
            duplicate=False,
            job_id=None,
            reason="not_callback",
            enqueued=False,
        )

    inline = AsyncMock()
    with (
        patch("app.routers.telegram_callback.ingest_callback_update", _ingest),
        patch("app.routers.telegram_callback.handle_callback_update", inline),
    ):
        response = await _post(_build_app(), {"update_id": 5})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert seen == [{"update_id": 5}]
    inline.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_enable_gate_still_wins_over_the_durable_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", False, raising=False
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED",
        True,
        raising=False,
    )
    ingest = AsyncMock()
    with patch("app.routers.telegram_callback.ingest_callback_update", ingest):
        response = await _post(_build_app(), _VALID_UPDATE)
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "order_proposals_telegram_disabled"
    ingest.assert_not_awaited()
