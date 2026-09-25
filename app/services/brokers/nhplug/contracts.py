"""Per-call mutation authorization for the NHPLUG mock order boundary.

Stage 2 consumes this contract at exactly one place: the client's mutation
dispatcher.  An order request is sent only for ``dry_run=False`` together with
``confirm=True`` (exact booleans), and only while ``NHPLUG_MOCK_ENABLED`` is
armed.  ``dry_run=True`` never dispatches, whatever ``confirm`` says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal


@dataclass(frozen=True, slots=True)
class DryRunConfirmContract:
    """A mutation must be dry-run or have explicit confirmation."""

    dry_run: bool = True
    confirm: bool = False

    def assert_dispatch_allowed(self) -> None:
        """Reject a non-dry action unless its caller confirms it."""

        if not self.dry_run and not self.confirm:
            raise ValueError("non-dry NHPLUG actions require confirm=True")

    @property
    def authorizes_send(self) -> bool:
        """True only for the exact ``dry_run=False`` + ``confirm=True`` pair."""

        return self.dry_run is False and self.confirm is True


# --- committed ledger intent (#711 tester round 2) -------------------------
#
# "No ledger row, no send" is enforced at the client itself: every order
# dispatch needs a CommittedOrderIntent, which only the ledger service issues
# after the ``submitting`` row is committed.  The issuer proof below is private;
# a static guard allows ``issue_committed_intent`` to be called from
# ``app/services/nhplug_mock/ledger_service.py`` only.  This is accidental-
# prevention plus static detection, like the rest of the boundary.

_LEDGER_ISSUER: Final[object] = object()
IntentOperation = Literal["place", "modify", "cancel"]


@dataclass(frozen=True, slots=True)
class CommittedOrderIntent:
    """A committed ledger row's order parameters, consumable exactly once."""

    ledger_row_id: int
    client_request_id: str
    operation: IntentOperation
    symbol: str
    side: str | None
    quantity: int | None
    price: int | None
    original_order_no: int | None
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _LEDGER_ISSUER:
            raise ValueError("order intents are issued only by the ledger service")

    @property
    def is_ledger_issued(self) -> bool:
        return self._issuer is _LEDGER_ISSUER


def issue_committed_intent(
    *,
    ledger_row_id: int,
    client_request_id: str,
    operation: IntentOperation,
    symbol: str,
    side: str | None,
    quantity: int | None,
    price: int | None,
    original_order_no: int | None,
) -> CommittedOrderIntent:
    """Ledger-service only (static guard): mint an intent for a committed row."""

    return CommittedOrderIntent(
        ledger_row_id=ledger_row_id,
        client_request_id=client_request_id,
        operation=operation,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        original_order_no=original_order_no,
        _issuer=_LEDGER_ISSUER,
    )
