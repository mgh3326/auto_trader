"""Durable two-state KIS mock trading kill switch.

The existing ``KISCircuitBreaker`` is a network/retry breaker.  It is not
imported here: this control plane owns the separate trading-mutation policy.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from sqlalchemy import text

logger = logging.getLogger(__name__)


class KillMode(StrEnum):
    ACTIVE = "ACTIVE"
    ENTRY_HALT = "ENTRY_HALT"
    GLOBAL_FREEZE = "GLOBAL_FREEZE"


@dataclass(frozen=True)
class KillSwitchState:
    mode: KillMode
    reason: str
    updated_by: str
    updated_at: datetime | None = None
    read_failed: bool = False


class KillSwitchReadError(RuntimeError):
    """The durable state could not be read or was structurally invalid."""


class KillSwitchRearmUnauthorized(RuntimeError):
    """The only re-arm route is an operator CLI with both gates supplied."""


class KillSwitchStore(Protocol):
    async def read(self) -> KillSwitchState: ...

    async def set_mode(
        self, *, mode: KillMode, reason: str, updated_by: str
    ) -> KillSwitchState: ...


class PostgresKillSwitchStore:
    """Control-table service; all writes remain inside this service layer."""

    _TABLE = "review.kis_mock_runner_control"

    async def read(self) -> KillSwitchState:
        from app.core.db import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        "SELECT mode, reason, updated_by, updated_at "
                        f"FROM {self._TABLE} WHERE id = 1"
                    )
                )
                row = result.mappings().one_or_none()
        except Exception as exc:  # noqa: BLE001 - caller must fail closed
            raise KillSwitchReadError("kill control table read failed") from exc
        if row is None:
            raise KillSwitchReadError("kill control row is missing")
        try:
            return KillSwitchState(
                mode=KillMode(str(row["mode"])),
                reason=str(row["reason"]),
                updated_by=str(row["updated_by"]),
                updated_at=row["updated_at"],
            )
        except (TypeError, ValueError) as exc:
            raise KillSwitchReadError(
                "kill control row contains an invalid mode"
            ) from exc

    async def set_mode(
        self, *, mode: KillMode, reason: str, updated_by: str
    ) -> KillSwitchState:
        if not reason.strip() or not updated_by.strip():
            raise ValueError("kill switch reason and updated_by must be non-blank")
        from app.core.db import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        f"UPDATE {self._TABLE} "
                        "SET mode = :mode, reason = :reason, "
                        "updated_by = :updated_by, updated_at = now() "
                        "WHERE id = 1 RETURNING mode, reason, updated_by, updated_at"
                    ),
                    {
                        "mode": mode.value,
                        "reason": reason,
                        "updated_by": updated_by,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    await session.rollback()
                    raise KillSwitchReadError("kill control row is missing")
                await session.commit()
        except KillSwitchReadError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed for caller
            raise KillSwitchReadError("kill control table write failed") from exc
        return KillSwitchState(
            mode=KillMode(str(row["mode"])),
            reason=str(row["reason"]),
            updated_by=str(row["updated_by"]),
            updated_at=row["updated_at"],
        )


async def read_effective_kill_switch(store: KillSwitchStore) -> KillSwitchState:
    """Turn every durable-state read failure into an explicit GLOBAL_FREEZE."""
    try:
        return await store.read()
    except Exception:  # noqa: BLE001 - no failure may open a trading path
        logger.exception("KIS mock kill switch read failed; GLOBAL_FREEZE applied")
        return KillSwitchState(
            mode=KillMode.GLOBAL_FREEZE,
            reason="kill_control_read_failed",
            updated_by="fail_closed",
            read_failed=True,
        )


async def rearm_active(
    store: KillSwitchStore,
    *,
    operator_gate: bool,
    confirm: bool,
    updated_by: str,
) -> KillSwitchState:
    """The sole re-arm primitive, called only by the dedicated operator CLI."""
    if not operator_gate or not confirm or not updated_by.strip():
        raise KillSwitchRearmUnauthorized(
            "re-arm requires operator gate, explicit confirmation, and updated_by"
        )
    return await store.set_mode(
        mode=KillMode.ACTIVE,
        reason="operator_rearm_confirmed",
        updated_by=updated_by,
    )


AsyncStateSetter = Callable[[KillMode, str, str], Awaitable[KillSwitchState]]
