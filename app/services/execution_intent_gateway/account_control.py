"""hk 5441 — the account kill row (design 5438 I7 b).

``review.execution_account_control`` holds one row per gateway account. Only
``ACTIVE`` lets a submit through; a missing row, an unknown mode or a failed
read is a freeze. A cancel never reads it.

The store has the shape of ``kis_mock_runner.control.KillSwitchStore`` so the
same fail-closed reader (``read_effective_kill_switch``) serves both accounts.
The row is written by the operator command only (``scripts/execution_mandates.py
control``); the migration seeds it frozen.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_mandates import (
    ACCOUNT_CONTROL_MODES,
    ExecutionAccountControl,
)
from app.services.kis_mock_runner.control import (
    KillMode,
    KillSwitchReadError,
    KillSwitchState,
)

_SYSTEM_PREFIX: Final = "system:"


class AccountControlError(ValueError):
    """An account control command that cannot be applied; nothing was written."""


class PostgresAccountControlStore:
    """Reads one account's kill row. Raises on anything but a valid row."""

    def __init__(
        self, account: str, session_factory: Callable[[], AsyncSession]
    ) -> None:
        self._account = account
        self._session_factory = session_factory

    async def read(self) -> KillSwitchState:
        try:
            async with self._session_factory() as session:
                row = await session.scalar(
                    select(ExecutionAccountControl).where(
                        ExecutionAccountControl.account == self._account
                    )
                )
                await session.rollback()
        except Exception as exc:  # noqa: BLE001 - any read failure is a freeze
            raise KillSwitchReadError("account control read failed") from exc
        if row is None:
            raise KillSwitchReadError("account control row is missing")
        try:
            mode = KillMode(row.mode)
        except ValueError as exc:
            raise KillSwitchReadError("account control mode is unknown") from exc
        return KillSwitchState(
            mode=mode,
            reason=row.reason,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
        )

    async def set_mode(
        self, *, mode: KillMode, reason: str, updated_by: str
    ) -> KillSwitchState:
        async with self._session_factory() as session:
            state = await set_account_mode(
                session,
                account=self._account,
                mode=mode.value,
                reason=reason,
                actor=updated_by,
            )
            await session.commit()
        return state


async def set_account_mode(
    session: AsyncSession, *, account: str, mode: str, reason: str, actor: str
) -> KillSwitchState:
    """Set one account's mode. The caller commits."""
    if mode not in ACCOUNT_CONTROL_MODES:
        raise AccountControlError(
            "mode must be one of: " + ", ".join(ACCOUNT_CONTROL_MODES)
        )
    why = str(reason or "").strip()
    who = str(actor or "").strip()
    if not why or len(why) > 500:
        raise AccountControlError("reason is required (1-500 characters)")
    if not who or len(who) > 128 or who.startswith(_SYSTEM_PREFIX):
        raise AccountControlError("actor must be a person (1-128 characters)")
    result = await session.execute(
        update(ExecutionAccountControl)
        .where(ExecutionAccountControl.account == account)
        .values(mode=mode, reason=why, updated_by=who, updated_at=func.now())
        .returning(ExecutionAccountControl.updated_at)
    )
    updated_at = result.scalar_one_or_none()
    if updated_at is None:
        raise AccountControlError(f"account {account!r} has no control row")
    return KillSwitchState(
        mode=KillMode(mode), reason=why, updated_by=who, updated_at=updated_at
    )
