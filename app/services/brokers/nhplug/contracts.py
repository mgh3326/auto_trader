"""Per-call mutation authorization for the NHPLUG mock order boundary.

Stage 2 consumes this contract at exactly one place: the client's mutation
dispatcher.  An order request is sent only for ``dry_run=False`` together with
``confirm=True`` (exact booleans), and only while ``NHPLUG_MOCK_ENABLED`` is
armed.  ``dry_run=True`` never dispatches, whatever ``confirm`` says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


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


# --- durable single-use dispatch claim (#711 tester round 3) ----------------
#
# "No ledger row, no send" and "exactly one send of exactly the committed
# body" are enforced by the database, not by an in-process token:
#
# * the caller states what it expects to send (``ExpectedOrder``); this is a
#   match filter only and is never used to build the request body;
# * immediately before send, the client asks the ledger service to claim the
#   row with one atomic conditional UPDATE (submitting -> dispatching, WHERE
#   the id, client_request_id, every order field, status and an empty claim
#   all match), committed before any byte is sent;
# * the request body is built only from the claimed row returned by that
#   UPDATE (``ClaimedOrder``).  A second claim of the same row — replay,
#   another client, another process, a concurrent call — matches no row.

IntentOperation = Literal["place", "modify", "cancel"]


@dataclass(frozen=True, slots=True)
class ExpectedOrder:
    """What the caller intends to send; used only in the claim's WHERE."""

    operation: IntentOperation
    symbol: str
    side: str | None
    quantity: int | None
    price: int | None
    original_order_no: int | None


@dataclass(frozen=True, slots=True)
class ClaimedOrder:
    """Order values as returned by the committed atomic claim UPDATE."""

    ledger_row_id: int
    client_request_id: str
    claim_token: str
    operation: IntentOperation
    symbol: str
    side: str | None
    quantity: int | None
    price: int | None
    original_order_no: int | None
