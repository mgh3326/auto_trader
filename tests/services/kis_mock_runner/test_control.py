from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.kis_mock_runner.control import (
    KillMode,
    KillSwitchRearmUnauthorized,
    KillSwitchState,
    read_effective_kill_switch,
    rearm_active,
)
from app.services.kis_mock_runner.runner import MutationKind, mutation_permission


class FakeControlStore:
    def __init__(self, state: KillSwitchState | Exception) -> None:
        self.state = state
        self.writes: list[tuple[KillMode, str, str]] = []

    async def read(self) -> KillSwitchState:
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    async def set_mode(
        self, *, mode: KillMode, reason: str, updated_by: str
    ) -> KillSwitchState:
        self.writes.append((mode, reason, updated_by))
        state = KillSwitchState(
            mode=mode,
            reason=reason,
            updated_by=updated_by,
            updated_at=datetime.now(UTC),
        )
        self.state = state
        return state


@pytest.mark.asyncio
async def test_kill_read_failure_is_global_freeze() -> None:
    state = await read_effective_kill_switch(FakeControlStore(RuntimeError("db down")))
    assert state.mode is KillMode.GLOBAL_FREEZE
    assert state.read_failed is True
    assert state.reason == "kill_control_read_failed"


@pytest.mark.parametrize(
    ("mode", "allowed", "blocked"),
    [
        (KillMode.ACTIVE, set(MutationKind), set()),
        (
            KillMode.ENTRY_HALT,
            {
                MutationKind.EXIT,
                MutationKind.CANCEL_PENDING_ENTRY,
                MutationKind.CANCEL_EXIT,
                MutationKind.MODIFY_EXIT,
                MutationKind.RETRY_EXIT,
            },
            {
                MutationKind.ENTRY,
                MutationKind.MODIFY_ENTRY,
                MutationKind.RETRY_ENTRY,
            },
        ),
        (KillMode.GLOBAL_FREEZE, set(), set(MutationKind)),
    ],
)
def test_kill_state_transition_table(
    mode: KillMode, allowed: set[MutationKind], blocked: set[MutationKind]
) -> None:
    for kind in allowed:
        assert mutation_permission(mode, kind).allowed is True
    for kind in blocked:
        decision = mutation_permission(mode, kind)
        assert decision.allowed is False
        assert decision.reason == mode.value


@pytest.mark.asyncio
async def test_rearm_requires_both_operator_gate_and_confirm() -> None:
    store = FakeControlStore(
        KillSwitchState(
            mode=KillMode.GLOBAL_FREEZE,
            reason="operator_stop",
            updated_by="operator",
        )
    )
    with pytest.raises(KillSwitchRearmUnauthorized):
        await rearm_active(
            store, operator_gate=False, confirm=True, updated_by="operator-a"
        )
    with pytest.raises(KillSwitchRearmUnauthorized):
        await rearm_active(
            store, operator_gate=True, confirm=False, updated_by="operator-a"
        )
    state = await rearm_active(
        store, operator_gate=True, confirm=True, updated_by="operator-a"
    )
    assert state.mode is KillMode.ACTIVE
    assert store.writes == [(KillMode.ACTIVE, "operator_rearm_confirmed", "operator-a")]
