"""Future mutation contract, deliberately separate from this read-only stage.

The type records the project-wide rule that a non-dry action needs an explicit
per-call confirmation.  Stage 1 provides no dispatch method that can consume
this contract; the AST guard keeps it that way.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DryRunConfirmContract:
    """A future action must be dry-run or have explicit confirmation."""

    dry_run: bool = True
    confirm: bool = False

    def assert_dispatch_allowed(self) -> None:
        """Reject a future non-dry action unless its caller confirms it."""

        if not self.dry_run and not self.confirm:
            raise ValueError("non-dry NHPLUG actions require confirm=True")
