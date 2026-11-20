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
4. **The proof reaches the approval.** When the send provably made no request
   -- the fence refused after it had seen the claim and every earlier attempt
   proven unsent, or the counted attempt was recorded as proven unsent -- the
   call returns a :class:`CardProvenUnsent` instead of a result. The approval
   that made the call ends the rung on it (:func:`card_unsent_reason`), in its
   own transaction, without asking the broker about an order that cannot
   exist. Asking would turn a failed lookup into an ``unverified`` rung with an
   idempotency key, which is not the look of a card that was never sent, and
   the handoff would stay claimed for good. Whatever is not such a proof --
   an unexplained earlier attempt, a lost claim, a fence that could not
   commit, a record that could not be written, anything after the hook
   returned -- comes back as an ordinary result and is classified as before.

A proposal with no ``exit_handoff_id`` gets its ``place_order_fn`` back
unchanged, so no other flow runs any of this.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.services.brokers.kis.pre_send import PreSendFreshnessError

logger = logging.getLogger(__name__)

PlaceFn = Callable[..., Any]


@dataclass(frozen=True)
class CardProvenUnsent:
    """A card send that provably made no request, bound to the claim it was for.

    Only :func:`exit_handoff_place_fn`'s wrapper makes one, and it is a type of
    its own rather than a key in a result: no broker response and no other
    place function can produce it.
    """

    handoff_id: int
    proposal_id: uuid.UUID
    claim_count: int
    #: The closed refusal code, or the order layer's own error text.
    reason: str


def card_unsent_reason(group: Any, submit: Any) -> str | None:
    """The reason to end the rung on, when ``submit`` is this card's own proof.

    ``None`` for every ordinary result, and for a proof that names another
    proposal or handoff than the one being approved.
    """
    if not isinstance(submit, CardProvenUnsent):
        return None
    if (
        getattr(group, "exit_handoff_id", None) != submit.handoff_id
        or getattr(group, "proposal_id", None) != submit.proposal_id
    ):
        return None
    return submit.reason


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
        #: The hook's own refusal, when it was the hook that stopped the send.
        self.hook_refusal: str | None = None
        #: The claim the fence counted this attempt on.
        self.claim_count: int | None = None
        #: The claim a refusing fence saw with nothing counted and nothing
        #: unexplained (``ExitHandoffError.unsent_claim_count``).
        self.refused_unsent_claim_count: int | None = None

    async def fence(self) -> str | None:
        """Commit one send attempt. Returns the refusal code, or ``None``."""
        from app.services.strategy_sleeves.exit_handoffs import (
            ExitHandoffError,
            fence_card_send,
        )

        try:
            self.claim_count = await fence_card_send(
                self._session_factory,
                handoff_id=self._handoff_id,
                proposal_id=self._proposal_id,
                configured_account_ref=self._account_ref(),
                now=self._clock(),
            )
        except ExitHandoffError as exc:
            self.refused_unsent_claim_count = exc.unsent_claim_count
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
                self.hook_refusal = refusal
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

    async def record_proven_unsent(self) -> bool:
        """Record the counted attempt as unsent. ``True`` only when committed."""
        from app.services.strategy_sleeves.exit_handoffs import (
            record_card_proven_unsent,
        )

        if self.claim_count is None:
            return False
        return await record_card_proven_unsent(
            self._session_factory,
            handoff_id=self._handoff_id,
            proposal_id=self._proposal_id,
            claim_count=self.claim_count,
            now=self._clock(),
        )

    def proof(self, claim_count: int, reason: str) -> CardProvenUnsent:
        return CardProvenUnsent(
            handoff_id=self._handoff_id,
            proposal_id=self._proposal_id,
            claim_count=claim_count,
            reason=reason,
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
            if guard.refused_unsent_claim_count is not None:
                # ...and the fence saw, under its locks, that nothing this card
                # ever counted is unexplained: the card has provably not posted.
                return guard.proof(guard.refused_unsent_claim_count, refusal)
            return _refused_result(refusal)
        kwargs["pre_send_hook"] = guard.pre_send_hook(kwargs.get("pre_send_hook"))
        result: Any = None
        error: Exception | None = None
        try:
            outcome = place_order_fn(**kwargs)
            result = await outcome if inspect.isawaitable(outcome) else outcome
        except Exception as exc:  # noqa: BLE001 - re-raised unless proven unsent
            error = exc
        except BaseException:
            # Cancelled mid-call: still record what is known, then get out.
            if not guard.reached_post:
                await guard.record_proven_unsent()
            raise
        if guard.reached_post:
            # The request may have been made: whatever came back is what it is.
            if error is not None:
                raise error
            return result
        recorded = await guard.record_proven_unsent()
        if not recorded or guard.claim_count is None:
            # The attempt stays counted as possibly sent; nothing is claimed.
            if error is not None:
                raise error
            return result
        return guard.proof(guard.claim_count, _unsent_reason(guard, result, error))

    return place


def _unsent_reason(guard: _CardSendGuard, result: Any, error: Exception | None) -> str:
    """Why the counted attempt made no request, for the rung's void reason."""
    if guard.hook_refusal is not None:
        return guard.hook_refusal
    if error is not None:
        return str(error) or type(error).__name__
    if isinstance(result, dict):
        codes = result.get("reason_codes")
        if result.get("pre_send_blocked") is True and codes:
            return str(codes[0])
        return str(result.get("error") or "submit_rejected")
    return "submit_rejected"
