"""The send boundary of an exit handoff card (hk 5441, design 5438 S5 d, O1, O3).

A card that was claimed from an exit handoff sends through the same approval
path as every other card. This module wraps only its live broker call:

1. **Fence.** Before the call, ``fence_card_send`` commits one send attempt on
   the handoff in its own transaction, and refuses unless this card still holds
   the claim and the configured Upbit account is the mandate's. Without that
   committed count the call is not made. The count outlives a rollback of the
   approval, which is what lets a later reader tell "may have sent" from "never
   sent" (O1, O3).
2. **Pre-POST check.** The transport's pre-send hook runs the approval window
   gate as before, then checks the claim and the account once more, immediately
   before the request.
3. **Proof.** The order layer calls the pre-send hook immediately before the
   HTTP request and nowhere else. If the hook did not return normally, no
   request was made, and the attempt is recorded as proven unsent.

A proposal with no ``exit_handoff_id`` gets its ``place_order_fn`` back
unchanged, so no other flow runs any of this.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.services.brokers.kis.pre_send import PreSendFreshnessError

logger = logging.getLogger(__name__)

PlaceFn = Callable[..., Any]


class _CardSendGuard:
    def __init__(
        self,
        *,
        handoff_id: int,
        proposal_id: Any,
        session_factory: Callable[[], Any],
        account_ref: Callable[[], str | None],
        clock: Callable[[], datetime],
    ) -> None:
        self._handoff_id = handoff_id
        self._proposal_id = proposal_id
        self._session_factory = session_factory
        self._account_ref = account_ref
        self._clock = clock
        self.reached_post = False

    async def fence(self) -> str | None:
        """Commit one send attempt. Returns the refusal code, or ``None``."""
        from app.services.strategy_sleeves.exit_handoffs import (
            ExitHandoffError,
            fence_card_send,
        )

        try:
            await fence_card_send(
                self._session_factory,
                handoff_id=self._handoff_id,
                proposal_id=self._proposal_id,
                configured_account_ref=self._account_ref(),
                now=self._clock(),
            )
        except ExitHandoffError as exc:
            return exc.code
        except Exception as exc:  # noqa: BLE001 - no committed count, no send
            logger.error(
                "exit_handoff_card.fence_failed",
                extra={"exception_type": type(exc).__name__},
            )
            return "exit_handoff_fence_unavailable"
        return None

    def pre_send_hook(
        self, inner: Callable[[], Awaitable[None]] | None
    ) -> Callable[[], Awaitable[None]]:
        async def hook() -> None:
            if inner is not None:
                await inner()
            refusal = await self._refusal_before_post()
            if refusal is not None:
                raise PreSendFreshnessError((refusal,))
            self.reached_post = True

        return hook

    async def _refusal_before_post(self) -> str | None:
        from app.services.strategy_sleeves.exit_handoffs import card_refusal

        try:
            async with self._session_factory() as session:
                refusal = await card_refusal(
                    session,
                    handoff_id=self._handoff_id,
                    proposal_id=self._proposal_id,
                    configured_account_ref=self._account_ref(),
                )
                await session.rollback()
                return refusal
        except Exception as exc:  # noqa: BLE001 - an unreadable check sends nothing
            logger.error(
                "exit_handoff_card.pre_post_check_failed",
                extra={"exception_type": type(exc).__name__},
            )
            return "exit_handoff_check_unavailable"

    async def record_proven_unsent(self) -> None:
        from app.services.strategy_sleeves.exit_handoffs import (
            record_card_proven_unsent,
        )

        await record_card_proven_unsent(
            self._session_factory,
            handoff_id=self._handoff_id,
            proposal_id=self._proposal_id,
            now=self._clock(),
        )


def _refused_result(code: str) -> dict[str, Any]:
    """The shape ``_place_order_impl`` returns for a proven pre-send block."""
    return {
        "success": False,
        "pre_send_blocked": True,
        "reason_codes": [code],
        "error": code,
        "dry_run": False,
    }


def exit_handoff_place_fn(
    group: Any,
    place_order_fn: PlaceFn,
    *,
    session_factory: Callable[[], Any] | None = None,
    account_ref: Callable[[], str | None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PlaceFn:
    """``place_order_fn`` for this proposal: unchanged unless it is a handoff card."""
    handoff_id = getattr(group, "exit_handoff_id", None)
    if handoff_id is None:
        return place_order_fn

    if session_factory is None:
        from app.core.db import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    if account_ref is None:
        from app.services.strategy_sleeves.account_binding import (
            configured_upbit_account_ref,
        )

        account_ref = configured_upbit_account_ref

    async def place(**kwargs: Any) -> Any:
        if kwargs.get("dry_run") is not False:
            # The preview reaches no order endpoint; it is not a send.
            outcome = place_order_fn(**kwargs)
            return await outcome if inspect.isawaitable(outcome) else outcome
        guard = _CardSendGuard(
            handoff_id=handoff_id,
            proposal_id=group.proposal_id,
            session_factory=session_factory,
            account_ref=account_ref,
            clock=clock or (lambda: datetime.now(UTC)),
        )
        refusal = await guard.fence()
        if refusal is not None:
            # Nothing was counted, so nothing may be sent.
            return _refused_result(refusal)
        kwargs["pre_send_hook"] = guard.pre_send_hook(kwargs.get("pre_send_hook"))
        try:
            outcome = place_order_fn(**kwargs)
            return await outcome if inspect.isawaitable(outcome) else outcome
        finally:
            if not guard.reached_post:
                await guard.record_proven_unsent()

    return place
