"""Send the initial Telegram approval message for a proposal (ROB-816 PR 2).

``send_proposal_for_approval`` is a top-level caller module, same as
``telegram_callback.py`` -- it opens and COMMITS its own DB session rather
than being constructor-injected, because it (a) is invoked from
``order_proposal_create`` after that tool's own session has already closed
and committed, and (b) calls the Telegram notifier, which
``OrderProposalsService``/``OrderProposalRepository`` never do (they only
flush -- see ``service.py``'s module docstring).

Commit-before-notify is not a live risk here the way it is in
``telegram_callback.py`` (there is no notify call *after* this function's
mutating work), but the nonce mint + ``source_asof`` merge are still
committed explicitly before returning, matching that module's established
discipline rather than relying on implicit ``async with`` behavior.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.order_proposals.approval_message import build_approval_message
from app.services.order_proposals.service import OrderProposalsService

logger = logging.getLogger(__name__)

ServiceFactory = Callable[[], Any]


def _generate_nonce() -> str:
    # Duplicated from telegram_callback.py::_generate_nonce (2 lines) rather
    # than imported -- that name is `_`-prefixed/module-private, and this
    # module is a peer top-level caller, not a consumer of that module.
    return secrets.token_urlsafe(12)


async def send_proposal_for_approval(
    proposal_id: uuid.UUID,
    *,
    notifier: Any,
    now: datetime,
    service_factory: ServiceFactory = AsyncSessionLocal,
) -> int | None:
    """Mint a fresh approval nonce, render the message, and send it.

    Sends to the FIRST entry in
    ``settings.order_proposals_telegram_chat_allowlist`` -- the return type
    is a single ``int | None`` message_id, which only makes sense for a
    single-chat send, not a broadcast. An empty allowlist is a no-op (no
    nonce mint, no send, returns ``None``) -- callers (the MCP wiring) should
    already gate on a non-empty allowlist before calling this, but this
    function defends independently.
    """
    allowlist = settings.order_proposals_telegram_chat_allowlist
    if not allowlist:
        return None
    chat_id = allowlist[0]

    async with service_factory() as session:
        service = OrderProposalsService(session)

        fresh_nonce = _generate_nonce()
        await service.set_approval_nonce(proposal_id, fresh_nonce)

        group, rungs = await service.get_proposal(proposal_id)
        text, keyboard = build_approval_message(group=group, rungs=rungs)

        message_id = await notifier.send_approval_message(
            text, keyboard, chat_id=chat_id
        )

        if message_id is not None:
            await service.record_approval_dispatch(
                proposal_id, message_id=message_id, chat_id=chat_id, now=now
            )

        # Commit explicitly before returning -- see module docstring. The
        # nonce mint above is committed even when message_id is None (send
        # failed): a fresh nonce with no message sent is not a correctness
        # problem, it just means the operator can't approve yet.
        await session.commit()
        return message_id


__all__ = ["send_proposal_for_approval"]
