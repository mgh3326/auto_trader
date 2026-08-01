"""Ports that consume the neutral mutation command/outcome contract."""

from __future__ import annotations

from typing import Protocol

from app.services.execution_outcomes.contract import MutationCommand, MutationOutcome


class BrokerMutationPort(Protocol):
    """Broker adapter boundary for a future caller-by-caller migration.

    B-1 provides only this structural contract.  It intentionally supplies no
    implementation and performs no caller cutover.
    """

    async def execute(self, command: MutationCommand, /) -> MutationOutcome: ...


__all__ = ["BrokerMutationPort"]
