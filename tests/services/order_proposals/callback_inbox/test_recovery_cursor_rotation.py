"""W5 R34 correction — durable cross-tier recovery rotation.

R34 separated malformed active budgets from the normal recovery tiers, but its
local ordering always began at the malformed tier.  The recovery loop stops as
soon as it has spent ``limit`` execution slots, so persistent malformed and
exhausted heads made queued/stale work unreachable forever for ``limit`` one
or two.  The ordering position must therefore be durable database state, not
a clock value or process-local cursor.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import inspect
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import attempt_budget_poison_rows, make_update

pytestmark = pytest.mark.integration


_ROW_FIELDS = frozenset(
    {
        "state",
        "attempt_count",
        "max_attempts",
        "available_at",
        "started_at",
        "handler_entered_at",
        "handler_completed_at",
        "terminal_state_pending",
        "outcome",
        "error_class",
    }
)

_TIER_NAMES = {
    0: "malformed",
    1: "exhausted",
    2: "queued",
    3: "stale",
}


def _synthetic_data() -> str:
    from app.services.order_proposals.approval_message import build_callback_data
    from app.services.order_proposals.dispatch_contract import (
        ApprovalCardKind,
        DispatchBinding,
        build_membership_digest,
    )

    proposal_id = uuid.uuid4()
    nonce = "nonce123456"
    return build_callback_data(
        action="op",
        proposal_id=proposal_id,
        nonce=nonce,
        binding=DispatchBinding(
            attempt_id=uuid.uuid4(),
            card_kind=ApprovalCardKind.MANUAL,
            membership_revision=1,
            membership_digest=build_membership_digest(
                card_kind=ApprovalCardKind.MANUAL,
                membership_revision=1,
                members=[{"proposal_id": str(proposal_id), "approval_nonce": nonce}],
            ),
        ),
    )


async def _queue(inbox_cleanup: list[uuid.UUID], *, received_at: datetime) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 980_000 + uuid.uuid4().int % 10_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(
            data=_synthetic_data(),
            update_id=update_id,
            callback_id=f"cursor-{update_id}-{uuid.uuid4().hex[:8]}",
        ),
        now=received_at,
        enqueue_fn=_no_kick,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


async def _set_row(job_id: uuid.UUID, **fields: Any) -> None:
    """Test-owned raw update; never use a shipped ``*_for_test`` mutator."""
    unknown = set(fields) - _ROW_FIELDS
    assert not unknown, sorted(unknown)
    assert fields
    assignments = ", ".join(f"{field} = :{field}" for field in sorted(fields))
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.text(
                "UPDATE review.telegram_callback_inbox "
                f"SET {assignments} WHERE job_id = :job_id"
            ),
            {"job_id": job_id, **fields},
        )
        assert result.rowcount == 1
        await session.commit()


@contextlib.asynccontextmanager
async def _persistent_four_tier_backlog(
    inbox_cleanup: list[uuid.UUID], *, now: datetime
) -> AsyncIterator[dict[uuid.UUID, int]]:
    """Keep one real candidate in every tier without letting it drain.

    The injected process function below deliberately does not mutate these
    rows.  This replenishes every head across every sweep, so fixed ordering
    cannot look fair merely because the first malformed/exhausted row was
    terminalised and disappeared.
    """
    malformed = await _queue(inbox_cleanup, received_at=now - timedelta(minutes=4))
    exhausted = await _queue(inbox_cleanup, received_at=now - timedelta(minutes=3))
    queued = await _queue(inbox_cleanup, received_at=now - timedelta(minutes=2))
    stale = await _queue(inbox_cleanup, received_at=now - timedelta(minutes=1))

    async with attempt_budget_poison_rows() as poison:
        await poison.insert(
            malformed,
            state="pending",
            attempt_count=3,
            max_attempts=4,
            available_at=now,
        )
        await poison.insert(
            exhausted,
            state="retry_wait",
            attempt_count=3,
            max_attempts=3,
            error_class="pre_core_failure",
            available_at=now + timedelta(hours=6),
        )
        await _set_row(
            stale,
            state="processing",
            attempt_count=1,
            started_at=now - timedelta(hours=6),
        )
        await poison.enforce_for_processing()
        yield {
            malformed: 0,
            exhausted: 1,
            queued: 2,
            stale: 3,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(("limit", "sweeps"), ((1, 4), (2, 2)))
async def test_persistent_four_tier_backlog_rotates_across_fresh_sweeps(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    limit: int,
    sweeps: int,
) -> None:
    """Each durable cyclic block offers every persistent tier within its bound."""
    from app.services.order_proposals.callback_inbox import recovery as recovery_module
    from app.services.order_proposals.callback_inbox.contracts import recovery_scan_cap

    initial = now_kst()
    clocks = (
        initial,
        initial + timedelta(days=4),
        initial + timedelta(minutes=1),
        initial + timedelta(days=8),
    )
    offered: list[int] = []
    reports: list[dict[str, Any]] = []

    async with _persistent_four_tier_backlog(inbox_cleanup, now=initial) as tiers:

        async def _persistent_process(
            job_id: uuid.UUID, **kwargs: Any
        ) -> dict[str, str]:
            offered.append(tiers[job_id])
            # A malformed worker error is intentionally persistent. It must
            # not pin future sweeps at tier zero.
            return {"status": "error" if tiers[job_id] == 0 else "succeeded"}

        for tick in range(sweeps):
            # Fresh module + fresh service/session every time.  The two
            # clock aliases and far-apart instants make a time-derived cursor
            # unable to satisfy this contract.
            fresh = importlib.reload(recovery_module)
            reports.append(
                await fresh.recover_callback_jobs(
                    process_fn=_persistent_process,
                    now_fn=lambda tick=tick: clocks[tick],
                    limit=limit,
                )
            )

    assert set(offered) == set(_TIER_NAMES), (
        "persistent tier heads starved under fixed emission order; "
        f"limit={limit}, offered={[_TIER_NAMES[tier] for tier in offered]}"
    )
    assert len(offered) == limit * sweeps
    for report in reports:
        assert report["claimed"] <= limit, report
        assert report["scanned"] <= recovery_scan_cap(limit), report


@pytest.mark.asyncio
async def test_a_repeated_malformed_processing_error_cannot_starve_queued_or_stale(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """The poison/error variant: tier zero stays runnable but not dominant."""
    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    now = now_kst()
    offered: list[int] = []
    async with _persistent_four_tier_backlog(inbox_cleanup, now=now) as tiers:

        async def _always_error_for_poison(
            job_id: uuid.UUID, **kwargs: Any
        ) -> dict[str, str]:
            tier = tiers[job_id]
            offered.append(tier)
            return {"status": "error" if tier == 0 else "succeeded"}

        for _ in range(4):
            fresh = importlib.reload(recovery_module)
            await fresh.recover_callback_jobs(
                process_fn=_always_error_for_poison,
                now_fn=lambda: now,
                limit=1,
            )

    assert 0 in offered
    assert {2, 3}.issubset(offered), (
        "a repeatedly failing malformed head prevented queued/stale progress; "
        f"offered={[_TIER_NAMES[tier] for tier in offered]}"
    )


async def _clear_cursor() -> None:
    from app.models.telegram_callback_inbox import TelegramCallbackRecoveryCursor

    async with AsyncSessionLocal() as session:
        await session.execute(sa.delete(TelegramCallbackRecoveryCursor))
        await session.commit()


async def _cursor_rows() -> list[tuple[int, int]]:
    from app.models.telegram_callback_inbox import TelegramCallbackRecoveryCursor

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                sa.select(
                    TelegramCallbackRecoveryCursor.id,
                    TelegramCallbackRecoveryCursor.next_tier,
                )
            )
        ).all()
        await session.rollback()
    return [(int(row.id), int(row.next_tier)) for row in rows]


@contextlib.asynccontextmanager
async def _isolated_cursor() -> AsyncIterator[None]:
    await _clear_cursor()
    try:
        yield
    finally:
        await _clear_cursor()


@pytest.mark.asyncio
async def test_atomic_l1_reservations_cover_every_start_once(
    _bootstrap_test_schema,
) -> None:
    from app.services.order_proposals.callback_inbox.recovery import (
        reserve_recovery_tier_block,
    )

    async with _isolated_cursor():
        assert await _cursor_rows() == []
        starts = await asyncio.gather(
            *(reserve_recovery_tier_block(limit=1) for _ in range(4))
        )
        assert await _cursor_rows() == [(1, 0)]

    assert set(starts) == {0, 1, 2, 3}
    assert len(starts) == len(set(starts))


@pytest.mark.asyncio
async def test_recovery_rejects_a_nonpositive_execution_limit_before_reserving_or_scanning(
    _bootstrap_test_schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.order_proposals.callback_inbox import repository as repo_module
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    scans: list[int] = []
    original = repo_module.CallbackInboxRepository.claimable_job_ids

    async def _scan_spy(self, **kwargs: Any):
        scans.append(1)
        return await original(self, **kwargs)

    monkeypatch.setattr(
        repo_module.CallbackInboxRepository,
        "claimable_job_ids",
        _scan_spy,
        raising=True,
    )
    with pytest.raises(ValueError, match="limit"):
        await recover_callback_jobs(limit=0)
    assert scans == []


@pytest.mark.asyncio
async def test_atomic_l2_reservations_are_disjoint_and_cover_the_ring(
    _bootstrap_test_schema,
) -> None:
    from app.services.order_proposals.callback_inbox.recovery import (
        reserve_recovery_tier_block,
    )

    async with _isolated_cursor():
        starts = await asyncio.gather(
            reserve_recovery_tier_block(limit=2), reserve_recovery_tier_block(limit=2)
        )

    windows = [{(start + offset) % 4 for offset in range(2)} for start in starts]
    assert len(starts) == len(set(starts)) == 2
    assert windows[0].isdisjoint(windows[1])
    assert set().union(*windows) == {0, 1, 2, 3}


@pytest.mark.asyncio
async def test_reservation_rollback_reuses_the_same_start_but_commit_burns_it(
    _bootstrap_test_schema,
) -> None:
    from app.services.order_proposals.callback_inbox.recovery import (
        reserve_recovery_tier_block,
    )
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    async with _isolated_cursor():
        async with AsyncSessionLocal() as session:
            start = await CallbackInboxService(session).reserve_recovery_tier_block(
                limit=1
            )
            assert start == 0
            await session.rollback()

        assert await reserve_recovery_tier_block(limit=1) == 0
        # A committed reservation may be followed by process death/no scan;
        # consuming that block is safe and the next sweep must advance.
        assert await reserve_recovery_tier_block(limit=1) == 1


@pytest.mark.asyncio
async def test_scan_rollback_cannot_roll_back_an_already_committed_reservation(
    _bootstrap_test_schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.order_proposals.callback_inbox import repository as repo_module
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
        reserve_recovery_tier_block,
    )

    async def _scan_fails(self, **kwargs: Any):
        raise RuntimeError("scan exploded after reservation")

    monkeypatch.setattr(
        repo_module.CallbackInboxRepository,
        "claimable_job_ids",
        _scan_fails,
        raising=True,
    )
    async with _isolated_cursor():
        with pytest.raises(RuntimeError, match="scan exploded"):
            await recover_callback_jobs(limit=1)
        assert await reserve_recovery_tier_block(limit=1) == 1


@pytest.mark.asyncio
async def test_reservation_commit_failure_runs_no_scan_or_handler_and_has_no_fallback(
    _bootstrap_test_schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.order_proposals.callback_inbox import repository as repo_module
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    scans: list[int] = []
    handlers: list[int] = []
    original_scan = repo_module.CallbackInboxRepository.claimable_job_ids

    async def _scan_spy(self, **kwargs: Any):
        scans.append(1)
        return await original_scan(self, **kwargs)

    @contextlib.asynccontextmanager
    async def _commit_fails() -> AsyncIterator[Any]:
        async with AsyncSessionLocal() as session:

            async def _fail_commit() -> None:
                raise RuntimeError("cursor commit failed")

            session.commit = _fail_commit  # type: ignore[method-assign]
            yield session

    async def _handler(*args: Any, **kwargs: Any) -> dict[str, str]:
        handlers.append(1)
        return {"status": "succeeded"}

    monkeypatch.setattr(
        repo_module.CallbackInboxRepository,
        "claimable_job_ids",
        _scan_spy,
        raising=True,
    )
    with pytest.raises(RuntimeError, match="cursor commit failed"):
        await recover_callback_jobs(
            limit=1,
            session_factory=_commit_fails,
            process_fn=_handler,
        )

    assert scans == []
    assert handlers == []


@pytest.mark.unit
def test_cursor_reservation_is_one_upsert_not_a_select_then_python_update() -> None:
    from app.services.order_proposals.callback_inbox.repository import (
        CallbackInboxRepository,
    )

    source = inspect.getsource(
        CallbackInboxRepository.reserve_recovery_tier_block
    ).strip()
    tree = ast.parse(source)
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "on_conflict_do_update" in calls
    assert "returning" in calls
    assert "select" not in calls
    assert "postgresql.insert" in source


@pytest.mark.asyncio
async def test_cursor_reservation_executes_one_database_statement(
    _bootstrap_test_schema,
) -> None:
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    async with _isolated_cursor():
        statements: list[str] = []
        async with AsyncSessionLocal() as session:
            original_execute = session.execute

            async def _recording(statement: Any, *args: Any, **kwargs: Any):
                statements.append(str(statement))
                return await original_execute(statement, *args, **kwargs)

            session.execute = _recording  # type: ignore[method-assign]
            assert (
                await CallbackInboxService(session).reserve_recovery_tier_block(limit=1)
                == 0
            )
            await session.rollback()

    assert len(statements) == 1
    assert (
        "insert into review.telegram_callback_recovery_cursor" in statements[0].lower()
    )
    assert "on conflict" in statements[0].lower()
    assert "returning" in statements[0].lower()


@pytest.mark.unit
def test_recovery_modules_carry_no_module_class_or_closure_cursor_state() -> None:
    """Durable ordering may live only in the singleton database row."""
    from app.services.order_proposals.callback_inbox import (
        recovery,
        repository,
        service,
    )

    for module in (recovery, repository, service):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            assert not isinstance(node, ast.Global | ast.Nonlocal), ast.dump(node)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and "cursor" in target.id.casefold()
                    ):
                        pytest.fail(
                            f"{module.__name__} stores cursor state locally: {target.id}"
                        )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if "cursor" in node.target.id.casefold():
                    pytest.fail(
                        f"{module.__name__} stores cursor state locally: {node.target.id}"
                    )

    recovery_source = inspect.getsource(recovery.recover_callback_jobs)
    assert "reserve_recovery_tier_block" in recovery_source
    assert "tier_start=" in recovery_source


@pytest.mark.asyncio
async def test_malformed_and_exhausted_rows_never_enter_the_handler_at_any_offset(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    from app.models.telegram_callback_inbox import TelegramCallbackRecoveryCursor
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    now = now_kst()
    handler_calls: list[int] = []
    async with attempt_budget_poison_rows() as poison:
        for _offset in range(4):
            malformed = await _queue(inbox_cleanup, received_at=now)
            exhausted = await _queue(inbox_cleanup, received_at=now)
            await poison.insert(
                malformed,
                state="pending",
                attempt_count=3,
                max_attempts=4,
                available_at=now,
            )
            await poison.insert(
                exhausted,
                state="retry_wait",
                attempt_count=3,
                max_attempts=3,
                error_class="pre_core_failure",
                available_at=now + timedelta(hours=1),
            )
        await poison.enforce_for_processing()
        async with _isolated_cursor():
            for offset in range(4):
                async with AsyncSessionLocal() as session:
                    session.add(
                        TelegramCallbackRecoveryCursor(
                            id=1,
                            next_tier=offset,
                            updated_at=now,
                        )
                    )
                    await session.commit()

                async def _handler(*args: Any, **kwargs: Any) -> dict[str, str]:
                    handler_calls.append(1)
                    return {"handled": True, "reason": "approved"}

                report = await recover_callback_jobs(handler=_handler, limit=1)
                assert report["claimed"] == 1
                await _clear_cursor()

    assert handler_calls == []
