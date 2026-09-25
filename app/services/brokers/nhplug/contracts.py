"""Per-call mutation authorization for the NHPLUG mock order boundary.

Stage 2 consumes this contract at exactly one place: the client's mutation
dispatcher.  An order request is sent only for ``dry_run=False`` together with
``confirm=True`` (exact booleans), and only while ``NHPLUG_MOCK_ENABLED`` is
armed.  ``dry_run=True`` never dispatches, whatever ``confirm`` says.
"""

from __future__ import annotations

from dataclasses import dataclass


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
