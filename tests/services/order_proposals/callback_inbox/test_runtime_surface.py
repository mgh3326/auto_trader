"""R31 — shipped callback-inbox APIs must exclude test-only seams.

The worker's lock and inbox service are production authority boundaries.  Test
shaping and observation belong below ``tests/`` so a future caller cannot use
them as a runtime mutation or lock-introspection escape hatch.
"""

from __future__ import annotations

import inspect
import uuid
from types import ModuleType
from typing import Any

import pytest

from app.services.order_proposals.callback_inbox.contracts import SCRUBBED_ON_TERMINAL

from .conftest import (
    _TEST_OWNED_INBOX_SHAPE_FIELDS,
    _TEST_OWNED_TERMINAL_SCRUB_ONLY_FIELDS,
    shape_owned_callback_inbox_row,
)

pytestmark = pytest.mark.unit


_REMOVED_RUNTIME_SURFACES = (
    ("locks module", "quarantined_handles"),
    ("PostgresJobAdvisoryLock", "closed"),
    ("PostgresJobAdvisoryLock", "connection_for_test"),
    ("PostgresJobAdvisoryLock", "backend_pid"),
    ("PostgresJobAdvisoryLock", "commit_for_test"),
    ("PostgresJobAdvisoryLock", "simulate_process_death"),
    ("CallbackInboxService", "force_state_for_test"),
)

_SERVICE_METHODS = frozenset(
    {
        "enqueue",
        "get_by_update_digest",
        "get",
        "classify_claim",
        "begin_attempt",
        "mark_handler_entered",
        "record_handler_verdict",
        "finalize",
        "schedule_retry",
        "reserve_recovery_tier_block",
        "claimable_job_ids",
        "backlog",
    }
)


def _public_methods(cls: type[Any]) -> set[str]:
    return {
        name
        for name, value in vars(cls).items()
        if not name.startswith("_")
        and (inspect.isfunction(value) or isinstance(value, property))
    }


def _public_module_functions(module: ModuleType) -> set[str]:
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and value.__module__ == module.__name__
    }


def _runtime_surface(owner: str) -> ModuleType | type[Any]:
    from app.services.order_proposals.callback_inbox import locks
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    return {
        "locks module": locks,
        "PostgresJobAdvisoryLock": locks.PostgresJobAdvisoryLock,
        "CallbackInboxService": CallbackInboxService,
    }[owner]


@pytest.mark.parametrize(("owner", "name"), _REMOVED_RUNTIME_SURFACES)
def test_removed_test_only_surfaces_are_not_shipped(owner: str, name: str) -> None:
    """Each baseline failure identifies one public test seam to remove."""
    runtime_owner = _runtime_surface(owner)
    assert not hasattr(runtime_owner, name), f"{owner}.{name} is shipped"

    # Keep the final surface closed, not merely free of these spellings.  The
    # removed-name assertion above deliberately comes first so the baseline
    # RED report has exactly one causal failure for each of the seven seams.
    if owner == "locks module":
        assert isinstance(runtime_owner, ModuleType)
        assert _public_module_functions(runtime_owner) == {"job_advisory_lock"}
    elif owner == "PostgresJobAdvisoryLock":
        from app.services.order_proposals.callback_inbox import locks

        assert isinstance(runtime_owner, type)
        assert _public_methods(runtime_owner) == {"try_acquire", "release"}
        assert _public_methods(locks.JobAdvisoryLock) == {"try_acquire", "release"}
        assert not hasattr(locks, "_BACKEND_PID")
    else:
        assert isinstance(runtime_owner, type)
        assert _public_methods(runtime_owner) == _SERVICE_METHODS


def test_r34_durable_reservation_layers_remain_runtime_contract() -> None:
    """R31 must not mistake the R34 recovery API for a test-only seam."""
    from app.services.order_proposals.callback_inbox import recovery
    from app.services.order_proposals.callback_inbox.repository import (
        CallbackInboxRepository,
    )
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    layers = (
        CallbackInboxRepository.reserve_recovery_tier_block,
        CallbackInboxService.reserve_recovery_tier_block,
        recovery.reserve_recovery_tier_block,
    )
    assert all(inspect.iscoroutinefunction(layer) for layer in layers)
    assert "reserve_recovery_tier_block" in recovery.__all__


def test_test_owned_row_shaper_tracks_every_terminal_authority_field() -> None:
    """Future terminal-scrub additions cannot become generic test mutations."""
    assert _TEST_OWNED_TERMINAL_SCRUB_ONLY_FIELDS == frozenset(SCRUBBED_ON_TERMINAL)
    assert _TEST_OWNED_TERMINAL_SCRUB_ONLY_FIELDS <= _TEST_OWNED_INBOX_SHAPE_FIELDS


@pytest.mark.asyncio
async def test_test_owned_row_shaper_rejects_arbitrary_or_unowned_mutations() -> None:
    """The replacement helper is closed to arbitrary fields and foreign rows."""
    with pytest.raises(ValueError, match="unexpected inbox shape field"):
        await shape_owned_callback_inbox_row(
            None,  # type: ignore[arg-type] - rejection happens before session use
            uuid.uuid4(),
            update_digest="must never be test-shaped",
        )
    with pytest.raises(ValueError, match="subject_short"):
        await shape_owned_callback_inbox_row(
            None,  # type: ignore[arg-type] - scrub rejection happens before session use
            uuid.uuid4(),
            subject_short="must never re-arm authority",
        )
    with pytest.raises(PermissionError, match="inbox_cleanup-owned"):
        await shape_owned_callback_inbox_row(
            None,  # type: ignore[arg-type] - ownership rejection is first
            uuid.uuid4(),
            state="pending",
        )
