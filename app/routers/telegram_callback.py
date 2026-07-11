"""ROB-816 PR 2 — Telegram webhook HTTP transport for order-proposal approvals.

Wires the single Telegram webhook endpoint to
``app.services.order_proposals.telegram_callback.handle_callback_update``
(Task 14). Auth is delegated entirely to :class:`AuthMiddleware`, which
gates every path under ``/trading/api/telegram/`` on the
``ORDER_PROPOSALS_TELEGRAM_TOKEN`` shared secret supplied via Telegram's
``secret_token`` webhook mechanism (header name configurable via
``ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER``) — see
``AuthMiddleware.TELEGRAM_CALLBACK_PATH_PREFIX``.

Hard invariants:

* Auth-gated by ``ORDER_PROPOSALS_TELEGRAM_TOKEN`` at the middleware
  layer; an unconfigured token responds ``403``.
* When ``settings.ORDER_PROPOSALS_TELEGRAM_ENABLED`` is off, the endpoint
  short-circuits with ``503`` and a structured body — same shape as the
  Hermes HTTP transport's gate-off (``investment_hermes_http.py``).
* The chat allowlist (authz) lives inside ``handle_callback_update``
  itself, not here.
* ``handle_callback_update`` never raises (Task 14's fail-closed
  guarantee), so this endpoint always returns ``200 {"ok": True}`` once
  past the enable gate — Telegram's webhook contract only cares about the
  HTTP status, not the body.
* No broker/order mutation reachable from this router directly; every
  safety boundary is enforced inside ``handle_callback_update``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.core.config import settings
from app.core.timezone import now_kst
from app.services.order_proposals.telegram_callback import handle_callback_update

router = APIRouter(prefix="/trading/api/telegram", tags=["telegram-approval"])


def _gate_off_503() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "order_proposals_telegram_disabled",
            "hint": (
                "Set ORDER_PROPOSALS_TELEGRAM_ENABLED=true on the host to "
                "enable the Telegram approval webhook endpoint."
            ),
        },
    )


def _require_enabled() -> None:
    if not settings.ORDER_PROPOSALS_TELEGRAM_ENABLED:
        raise _gate_off_503()


@router.post("/callback")
async def telegram_callback(body: dict[str, Any]) -> dict[str, Any]:
    """Telegram webhook entrypoint for order-proposal approve/deny callbacks.

    Accepts the raw Telegram ``Update`` payload as a permissive ``dict``
    (Telegram's ``Update`` schema is large and evolving; this endpoint must
    not reject updates on unknown/new fields). Always returns
    ``{"ok": True}`` on 200 regardless of ``handle_callback_update``'s
    internal result — Telegram only inspects the HTTP status.
    """
    _require_enabled()
    await handle_callback_update(body, now=now_kst())
    return {"ok": True}
