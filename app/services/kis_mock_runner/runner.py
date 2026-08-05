"""Foreground, strategy-neutral KIS mock runner safety shell (KR-B0)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from .control import (
    KillMode,
    KillSwitchState,
    KillSwitchStore,
    PostgresKillSwitchStore,
    read_effective_kill_switch,
)
from .correlation import kis_mock_runner_correlation_id
from .envelope import (
    AccountEnvelopeSnapshot,
    EnvelopeDecision,
    OrderIntent,
    assert_no_envelope_overrides,
    evaluate_envelope,
)
from .gates import KISMockRunnerDisabled, assert_runner_enabled
from .notifications import DeliveryReport, DiscordNotifier
from .overlay import OverlayBinding, OverlayRequired, require_overlay
from .session import is_krx_regular_session
from .singleton import KISMockWriterLease

# A supervised foreground loop only; this is neither a task registration nor a
# cron cadence.  The constant is intentionally not a user-configurable option.
FOREGROUND_TICK_SECONDS = 60


class RunnerStatus(StrEnum):
    DEFAULT_DISABLED = "default_disabled"
    OVERLAY_REQUIRED = "OVERLAY_REQUIRED"
    ENTRY_HALT = "entry_halt"
    GLOBAL_FREEZE = "global_freeze"
    KRX_RTH_REQUIRED = "krx_rth_required"
    ENVELOPE_BLOCKED = "envelope_blocked"
    READY_NO_INTENT = "ready_no_intent"


class MutationKind(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"
    CANCEL_PENDING_ENTRY = "cancel_pending_entry"
    CANCEL_EXIT = "cancel_exit"
    MODIFY_ENTRY = "modify_entry"
    MODIFY_EXIT = "modify_exit"
    RETRY_ENTRY = "retry_entry"
    RETRY_EXIT = "retry_exit"


@dataclass(frozen=True)
class MutationPermission:
    allowed: bool
    reason: str | None = None


def mutation_permission(mode: KillMode, kind: MutationKind) -> MutationPermission:
    """State table for all automatic KIS mock mutation kinds.

    ENTRY_HALT lets attributed pending entries be cancelled and lets all exit
    activity proceed.  GLOBAL_FREEZE blocks every automatic mutation.
    """
    if mode is KillMode.ACTIVE:
        return MutationPermission(allowed=True)
    if mode is KillMode.GLOBAL_FREEZE:
        return MutationPermission(allowed=False, reason="GLOBAL_FREEZE")
    allowed_while_halted = {
        MutationKind.EXIT,
        MutationKind.CANCEL_PENDING_ENTRY,
        MutationKind.CANCEL_EXIT,
        MutationKind.MODIFY_EXIT,
        MutationKind.RETRY_EXIT,
    }
    if kind in allowed_while_halted:
        return MutationPermission(allowed=True)
    return MutationPermission(allowed=False, reason="ENTRY_HALT")


@dataclass(frozen=True)
class AttributedPendingEntry:
    """Only a real attribution chain may be cancelled by ENTRY_HALT."""

    order_id: str
    correlation_id: str
    strategy_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("order_id", self.order_id),
            ("correlation_id", self.correlation_id),
            ("strategy_id", self.strategy_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-blank")


class PendingEntryPort(Protocol):
    async def list_attributed_pending_entries(
        self,
    ) -> tuple[AttributedPendingEntry, ...]: ...

    async def cancel_pending_entry(self, entry: AttributedPendingEntry) -> None: ...


class EmptyPendingEntryPort:
    """B0's normal no-overlay state: no order list and therefore no mutation."""

    async def list_attributed_pending_entries(
        self,
    ) -> tuple[AttributedPendingEntry, ...]:
        return ()

    async def cancel_pending_entry(self, entry: AttributedPendingEntry) -> None:
        del entry
        raise AssertionError("B0 has no pending-entry cancellation adapter")


@dataclass(frozen=True)
class RunnerResult:
    status: RunnerStatus
    kill_switch: KillSwitchState | None = None
    canceled_pending_entries: int = 0
    correlation_id: str | None = None
    envelope: EnvelopeDecision | None = None
    notification_reports: tuple[DeliveryReport, ...] = field(default_factory=tuple)
    broker_calls: int = 0


class KISMockRunner:
    """The B0 shell: it can guard future intents but cannot invent one."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str],
        tag: str,
        overlay: OverlayBinding | None = None,
        kill_switch_store: KillSwitchStore | None = None,
        writer_lease: KISMockWriterLease | None = None,
        pending_entry_port: PendingEntryPort | None = None,
        notifier: DiscordNotifier | None = None,
    ) -> None:
        if not tag.strip():
            raise ValueError("runner tag must be non-blank")
        self._environment = environment
        self._tag = tag
        self._overlay = overlay
        self._kill_switch_store = kill_switch_store or PostgresKillSwitchStore()
        self._writer_lease = writer_lease or KISMockWriterLease()
        self._pending_entry_port = pending_entry_port or EmptyPendingEntryPort()
        self._notifier = notifier or DiscordNotifier(environment)

    async def run_once(self) -> RunnerResult:
        """Run one guarded tick; it never creates an order without an overlay."""
        try:
            # This must precede lock, DB, broker, and webhook work.
            assert_runner_enabled(self._environment)
        except KISMockRunnerDisabled:
            return RunnerResult(status=RunnerStatus.DEFAULT_DISABLED)
        assert_no_envelope_overrides(self._environment)

        async with self._writer_lease:
            state = await read_effective_kill_switch(self._kill_switch_store)
            if state.mode is KillMode.GLOBAL_FREEZE:
                reports = await self._notify_alert(
                    event="kis_mock_runner_global_freeze",
                    payload={"reason": state.reason, "read_failed": state.read_failed},
                )
                return RunnerResult(
                    status=RunnerStatus.GLOBAL_FREEZE,
                    kill_switch=state,
                    notification_reports=reports,
                )
            if state.mode is KillMode.ENTRY_HALT:
                cancelled = await self._cancel_attributed_pending_entries(state)
                reports = await self._notify_alert(
                    event="kis_mock_runner_entry_halt",
                    payload={
                        "reason": state.reason,
                        "cancelled_pending_entries": cancelled,
                    },
                )
                return RunnerResult(
                    status=RunnerStatus.ENTRY_HALT,
                    kill_switch=state,
                    canceled_pending_entries=cancelled,
                    notification_reports=reports,
                )
            try:
                require_overlay(self._overlay)
            except OverlayRequired:
                reports = await self._notify_lifecycle(
                    event="kis_mock_runner_overlay_required",
                    payload={"status": RunnerStatus.OVERLAY_REQUIRED.value},
                )
                return RunnerResult(
                    status=RunnerStatus.OVERLAY_REQUIRED,
                    kill_switch=state,
                    notification_reports=reports,
                )

            # An overlay can be bound only by KR-B1.  B0 deliberately does not
            # evaluate it or construct an intent; that would be a hidden default.
            reports = await self._notify_lifecycle(
                event="kis_mock_runner_ready_no_intent",
                payload={"status": RunnerStatus.READY_NO_INTENT.value},
            )
            return RunnerResult(
                status=RunnerStatus.READY_NO_INTENT,
                kill_switch=state,
                notification_reports=reports,
            )

    async def run_forever(self) -> RunnerResult:
        """Foreground supervised loop; terminal safety states stop cleanly."""
        while True:
            result = await self.run_once()
            if result.status in {
                RunnerStatus.DEFAULT_DISABLED,
                RunnerStatus.OVERLAY_REQUIRED,
                RunnerStatus.ENTRY_HALT,
                RunnerStatus.GLOBAL_FREEZE,
            }:
                return result
            await asyncio.sleep(FOREGROUND_TICK_SECONDS)

    async def evaluate_overlay_intent(
        self,
        *,
        intent: OrderIntent,
        snapshot: AccountEnvelopeSnapshot,
        now: datetime | None = None,
        decision_key: str,
    ) -> RunnerResult:
        """Validate a future overlay intent without dispatching a broker mutation.

        A B1 executor must call this immediately before its own KRX adapter.  It
        handles the RTH, kill state, locked envelope, and daily-loss durable
        ENTRY_HALT transition.  The return value intentionally carries
        ``broker_calls=0``; broker dispatch belongs to a later bound adapter.
        """
        assert_runner_enabled(self._environment)
        assert_no_envelope_overrides(self._environment)
        overlay = require_overlay(self._overlay)
        current = now or datetime.now(UTC)
        async with self._writer_lease:
            state = await read_effective_kill_switch(self._kill_switch_store)
            kind = MutationKind.ENTRY if intent.role == "entry" else MutationKind.EXIT
            permission = mutation_permission(state.mode, kind)
            if not permission.allowed:
                status = (
                    RunnerStatus.GLOBAL_FREEZE
                    if state.mode is KillMode.GLOBAL_FREEZE
                    else RunnerStatus.ENTRY_HALT
                )
                return RunnerResult(status=status, kill_switch=state)
            if intent.role == "entry" and not is_krx_regular_session(current):
                return RunnerResult(
                    status=RunnerStatus.KRX_RTH_REQUIRED, kill_switch=state
                )
            envelope = evaluate_envelope(intent=intent, snapshot=snapshot)
            if envelope.requires_entry_halt:
                state = await self._kill_switch_store.set_mode(
                    mode=KillMode.ENTRY_HALT,
                    reason="daily_loss_halt_threshold_reached",
                    updated_by="kis_mock_runner",
                )
            if not envelope.allowed:
                return RunnerResult(
                    status=RunnerStatus.ENVELOPE_BLOCKED,
                    kill_switch=state,
                    envelope=envelope,
                )
            correlation_id = kis_mock_runner_correlation_id(
                tag=self._tag,
                candidate_id=overlay.candidate_id,
                contract_hash=overlay.contract_hash,
                strategy_id=overlay.strategy_id,
                decision_key=decision_key,
            )
            return RunnerResult(
                status=RunnerStatus.READY_NO_INTENT,
                kill_switch=state,
                correlation_id=correlation_id,
                envelope=envelope,
            )

    async def _cancel_attributed_pending_entries(self, state: KillSwitchState) -> int:
        permission = mutation_permission(state.mode, MutationKind.CANCEL_PENDING_ENTRY)
        if not permission.allowed:
            return 0
        entries = await self._pending_entry_port.list_attributed_pending_entries()
        for entry in entries:
            # Dataclass validation makes attribution mandatory before the adapter
            # is ever asked to mutate.  There is no placeholder strategy path.
            await self._pending_entry_port.cancel_pending_entry(entry)
        return len(entries)

    async def _notify_lifecycle(
        self, *, event: str, payload: dict[str, object]
    ) -> tuple[DeliveryReport, ...]:
        lifecycle = await self._notifier.lifecycle(event=event, payload=payload)
        if lifecycle.delivered or lifecycle.skipped:
            return (lifecycle,)
        alert = await self._notifier.alert(
            event="kis_mock_runner_lifecycle_delivery_failure",
            payload={"event": event, "retry_recorded": lifecycle.retry_recorded},
        )
        return lifecycle, alert

    async def _notify_alert(
        self, *, event: str, payload: dict[str, object]
    ) -> tuple[DeliveryReport, ...]:
        return (await self._notifier.alert(event=event, payload=payload),)
