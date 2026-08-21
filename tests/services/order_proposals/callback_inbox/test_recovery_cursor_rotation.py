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
import json
import os
import pathlib
import subprocess
import sys
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

_REPO = pathlib.Path(__file__).resolve().parents[4]


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


async def _cursor_table_exists() -> bool:
    async with AsyncSessionLocal() as session:
        exists = bool(
            (
                await session.execute(
                    sa.text(
                        "SELECT to_regclass("
                        "'review.telegram_callback_recovery_cursor') IS NOT NULL"
                    )
                )
            ).scalar_one()
        )
        await session.rollback()
    return exists


async def _replace_cursor_next_tier(next_tier: int) -> None:
    """Test-owned singleton setup; this is ordering state, never job authority."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa.text("DELETE FROM review.telegram_callback_recovery_cursor WHERE id = 1")
        )
        await session.execute(
            sa.text(
                "INSERT INTO review.telegram_callback_recovery_cursor "
                "(id, next_tier, updated_at) VALUES (1, :next_tier, now())"
            ),
            {"next_tier": next_tier},
        )
        await session.commit()


async def _cursor_next_tier() -> int | None:
    async with AsyncSessionLocal() as session:
        value = (
            await session.execute(
                sa.text(
                    "SELECT next_tier FROM review.telegram_callback_recovery_cursor "
                    "WHERE id = 1"
                )
            )
        ).scalar_one_or_none()
        await session.rollback()
    return None if value is None else int(value)


async def _clear_raw_cursor_if_present() -> None:
    if not await _cursor_table_exists():
        return
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa.text("DELETE FROM review.telegram_callback_recovery_cursor WHERE id = 1")
        )
        await session.commit()


_FRESH_PROCESS_SWEEP = """
import asyncio
import json
import os

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.services.order_proposals.callback_inbox.recovery import recover_callback_jobs


async def _record_only(job_id, **kwargs):
    seen.append(str(job_id))
    return {"status": "succeeded"}


async def _main():
    report = await recover_callback_jobs(limit=1, process_fn=_record_only)
    async with AsyncSessionLocal() as session:
        next_tier = (
            await session.execute(
                sa.text(
                    "SELECT next_tier FROM review.telegram_callback_recovery_cursor "
                    "WHERE id = 1"
                )
            )
        ).scalar_one_or_none()
        await session.rollback()
    print(
        "R34_FRESH_SWEEP="
        + json.dumps(
            {
                "claimed": report["claimed"],
                "cursor_next_tier": next_tier,
                "pid": os.getpid(),
                "scanned": report["scanned"],
                "seen": seen,
            },
            sort_keys=True,
        )
    )


seen = []
asyncio.run(_main())
"""


def _fresh_process_sweep() -> dict[str, Any]:
    """A new interpreter cannot retain a module/class cursor from this test."""
    environment = dict(os.environ)
    prior_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_REPO) if not prior_path else f"{_REPO}{os.pathsep}{prior_path}"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and source
        [sys.executable, "-c", _FRESH_PROCESS_SWEEP],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"fresh recovery process failed ({completed.returncode})\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    markers = [
        line.removeprefix("R34_FRESH_SWEEP=")
        for line in completed.stdout.splitlines()
        if line.startswith("R34_FRESH_SWEEP=")
    ]
    assert len(markers) == 1, completed.stdout
    decoded = json.loads(markers[0])
    assert isinstance(decoded, dict), decoded
    return decoded


@pytest.mark.asyncio
async def test_fresh_process_sweeps_follow_the_committed_cursor_tier_order(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """Fresh interpreters prove recovery uses the DB cursor through selection."""
    from app.services.order_proposals.callback_inbox.contracts import recovery_scan_cap

    now = now_kst()
    async with _persistent_four_tier_backlog(inbox_cleanup, now=now) as tiers:
        assert set(tiers.values()) == set(_TIER_NAMES)
        # q=1 means the stored next tier is exactly this sweep's reserved
        # start. It is deliberately nonzero: a process-local sequence that
        # starts from zero cannot pass by accident.
        await _replace_cursor_next_tier(2)
        try:
            reports = [_fresh_process_sweep(), _fresh_process_sweep()]
            for report, expected_start, expected_next in zip(
                reports, (2, 3), (3, 0), strict=True
            ):
                assert report["claimed"] == 1, report
                assert report["scanned"] <= recovery_scan_cap(1), report
                assert len(report["seen"]) == 1, report
                assert tiers[uuid.UUID(report["seen"][0])] == expected_start, report
                assert report["cursor_next_tier"] == expected_next, report
                assert await _cursor_next_tier() == expected_next
            assert reports[0]["pid"] != reports[1]["pid"], reports
        finally:
            await _clear_raw_cursor_if_present()


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
@pytest.mark.parametrize("limit", (0, -1))
async def test_recovery_rejects_a_nonpositive_execution_limit_before_reserving_or_scanning(
    _bootstrap_test_schema, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    from app.services.order_proposals.callback_inbox import recovery as recovery_module
    from app.services.order_proposals.callback_inbox import repository as repo_module

    reservations: list[int] = []
    scans: list[int] = []
    handlers: list[int] = []
    original = repo_module.CallbackInboxRepository.claimable_job_ids

    async def _reserve_spy(*args: Any, **kwargs: Any) -> int:
        reservations.append(1)
        return 0

    async def _scan_spy(self, **kwargs: Any):
        scans.append(1)
        return await original(self, **kwargs)

    async def _handler(*args: Any, **kwargs: Any) -> dict[str, str]:
        handlers.append(1)
        return {"status": "succeeded"}

    monkeypatch.setattr(
        recovery_module,
        "reserve_recovery_tier_block",
        _reserve_spy,
        raising=False,
    )
    monkeypatch.setattr(
        repo_module.CallbackInboxRepository,
        "claimable_job_ids",
        _scan_spy,
        raising=True,
    )
    cursor_exists = False
    before: list[tuple[int, int]] = []
    async with AsyncSessionLocal() as session:
        cursor_exists = bool(
            (
                await session.execute(
                    sa.text(
                        "SELECT to_regclass("
                        "'review.telegram_callback_recovery_cursor') IS NOT NULL"
                    )
                )
            ).scalar_one()
        )
        if cursor_exists:
            await session.execute(
                sa.text(
                    "DELETE FROM review.telegram_callback_recovery_cursor WHERE id = 1"
                )
            )
            await session.execute(
                sa.text(
                    "INSERT INTO review.telegram_callback_recovery_cursor "
                    "(id, next_tier, updated_at) VALUES (1, 2, now())"
                )
            )
            await session.commit()
            before = [(1, 2)]
        else:
            await session.rollback()

    try:
        with pytest.raises(ValueError, match="limit"):
            await recovery_module.recover_callback_jobs(
                limit=limit,
                process_fn=_handler,
            )

        if cursor_exists:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        sa.text(
                            "SELECT id, next_tier FROM "
                            "review.telegram_callback_recovery_cursor ORDER BY id"
                        )
                    )
                ).all()
                await session.rollback()
            assert [(int(row.id), int(row.next_tier)) for row in rows] == before
    finally:
        if cursor_exists:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    sa.text(
                        "DELETE FROM review.telegram_callback_recovery_cursor "
                        "WHERE id = 1"
                    )
                )
                await session.commit()
    assert reservations == []
    assert scans == []
    assert handlers == []


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
@pytest.mark.parametrize("limit", (4, 1001))
async def test_reservation_caps_each_block_at_one_full_four_tier_window(
    _bootstrap_test_schema, limit: int
) -> None:
    """A large execution cap reserves the ring once, never an out-of-range stride."""
    from app.services.order_proposals.callback_inbox.recovery import (
        reserve_recovery_tier_block,
    )

    async with _isolated_cursor():
        start = await reserve_recovery_tier_block(limit=limit)
        assert start == 0
        assert {(start + offset) % 4 for offset in range(4)} == {0, 1, 2, 3}
        # q=min(limit, 4), so q=4 returns the singleton to its canonical
        # in-range value even for an execution limit that is much larger.
        assert await _cursor_rows() == [(1, 0)]
        assert await reserve_recovery_tier_block(limit=1) == 0


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


def _assignment_parts(node: ast.AST) -> tuple[ast.expr | None, list[ast.expr]]:
    if isinstance(node, ast.Assign):
        return node.value, list(node.targets)
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return node.value, [node.target]
    return None, []


def _direct_scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """Nodes owned by one lexical scope, excluding nested scopes."""
    nodes: list[ast.AST] = []

    def _visit(node: ast.AST) -> None:
        if node is not scope and isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
        ):
            return
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            _visit(child)

    for statement in getattr(scope, "body", []):
        _visit(statement)
    return nodes


def _mutable_custom_class_names(tree: ast.AST) -> set[str]:
    """Local classes whose instances mutate their own persistent-looking state."""
    mutable: set[str] = set()
    for class_node in (
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    ):
        for node in ast.walk(class_node):
            if isinstance(node, ast.AugAssign) and isinstance(
                node.target, ast.Attribute
            ):
                if isinstance(node.target.value, ast.Name) and node.target.value.id in {
                    "self",
                    "cls",
                }:
                    mutable.add(class_node.name)
                    break
            value, targets = _assignment_parts(node)
            if value is None:
                continue
            if any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in {"self", "cls"}
                for target in targets
            ):
                mutable.add(class_node.name)
                break
    return mutable


_STATEFUL_BUILTIN_FACTORIES = frozenset({"bytearray", "dict", "list", "set"})
_STATEFUL_IMPORTED_FACTORIES = frozenset(
    {
        "collections.Counter",
        "collections.defaultdict",
        "collections.deque",
        "itertools.accumulate",
        "itertools.count",
        "itertools.cycle",
        "itertools.repeat",
        "queue.Queue",
        "random.Random",
    }
)


def _import_bindings(tree: ast.AST) -> dict[str, str]:
    """Resolve only import aliases needed to recognise known state factories."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".", 1)[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name != "*":
                    bindings[alias.asname or alias.name] = (
                        f"{module}.{alias.name}".strip(".")
                    )
    return bindings


def _stateful_constructor(
    value: ast.expr | None,
    *,
    custom_classes: set[str],
    imports: dict[str, str],
) -> str | None:
    """Recognise a local/imported state factory without name-based guesses."""
    if not isinstance(value, ast.Call):
        return None
    if isinstance(value.func, ast.Name):
        if value.func.id in custom_classes:
            return value.func.id
        if value.func.id in _STATEFUL_BUILTIN_FACTORIES:
            return f"builtin {value.func.id}"
        origin = imports.get(value.func.id)
        if origin in _STATEFUL_IMPORTED_FACTORIES:
            return origin
    elif isinstance(value.func, ast.Attribute) and isinstance(
        value.func.value, ast.Name
    ):
        origin = imports.get(value.func.value.id)
        if origin is not None:
            qualified = f"{origin}.{value.func.attr}"
            if qualified in _STATEFUL_IMPORTED_FACTORIES:
                return qualified
    return None


def _direct_stateful_bindings(
    scope: ast.AST,
    *,
    custom_classes: set[str],
    imports: dict[str, str],
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in _direct_scope_nodes(scope):
        value, targets = _assignment_parts(node)
        constructor = _stateful_constructor(
            value,
            custom_classes=custom_classes,
            imports=imports,
        )
        if constructor is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = constructor
    return bindings


def _nested_functions(scope: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    nested: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def _visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                continue
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                nested.append(child)
                continue
            _visit(child)

    _visit(scope)
    return nested


def _rotation_state_offenders(source: str) -> list[str]:
    """Find actual process-local custom state, not names that merely say cursor."""
    tree = ast.parse(source)
    classes = _mutable_custom_class_names(tree)
    imports = _import_bindings(tree)
    offenders: list[str] = []
    scopes: list[ast.AST] = [tree]
    scopes.extend(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))

    for scope in scopes:
        kind = "module" if isinstance(scope, ast.Module) else f"class {scope.name}"
        for node in _direct_scope_nodes(scope):
            value, targets = _assignment_parts(node)
            constructor = _stateful_constructor(
                value,
                custom_classes=classes,
                imports=imports,
            )
            if constructor is None:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    offenders.append(
                        f"line {node.lineno}: {kind} binds mutable {constructor} as {target.id}"
                    )

    functions = tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )
    for function in functions:
        closed_over = _direct_stateful_bindings(
            function,
            custom_classes=classes,
            imports=imports,
        )
        if not closed_over:
            continue
        for nested in _nested_functions(function):
            loaded = {
                node.id
                for node in _direct_scope_nodes(nested)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            }
            for name in sorted(set(closed_over) & loaded):
                offenders.append(
                    f"line {nested.lineno}: {nested.name} closes over "
                    f"mutable {closed_over[name]} as {name}"
                )

    for node in ast.walk(tree):
        if isinstance(node, ast.Global | ast.Nonlocal):
            offenders.append(f"line {node.lineno}: {type(node).__name__.lower()} state")
    return offenders


def _function_named(
    tree: ast.AST, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    return next(
        (
            node
            for node in getattr(tree, "body", [])
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ),
        None,
    )


def _method_named(
    tree: ast.AST, *, class_name: str, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    owner = next(
        (
            node
            for node in getattr(tree, "body", [])
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    if owner is None:
        return None
    return next(
        (
            node
            for node in owner.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ),
        None,
    )


def _call_keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next(
        (keyword.value for keyword in call.keywords if keyword.arg == name), None
    )


def _target_mentions_name(target: ast.AST, name: str) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id == name for node in ast.walk(target)
    )


def _tier_start_store_offenders(
    recover: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    initial_targets: set[int],
) -> list[str]:
    """The reservation result is immutable local data until the scan call."""
    offenders: list[str] = []
    for node in _direct_scope_nodes(recover):
        if (
            isinstance(node, ast.Name)
            and node.id == "tier_start"
            and isinstance(node.ctx, ast.Store | ast.Del)
            and id(node) not in initial_targets
        ):
            offenders.append(
                f"line {node.lineno}: tier_start is rebound or deleted after reservation"
            )
        elif isinstance(node, ast.ExceptHandler) and node.name == "tier_start":
            offenders.append(
                f"line {node.lineno}: tier_start is rebound by an exception handler"
            )
        elif isinstance(node, ast.alias) and node.asname == "tier_start":
            offenders.append(
                f"line {node.lineno}: tier_start is rebound by an import alias"
            )
        elif (
            isinstance(node, ast.MatchAs | ast.MatchStar) and node.name == "tier_start"
        ):
            offenders.append(
                f"line {node.lineno}: tier_start is rebound by a match pattern"
            )
        elif (
            isinstance(node, ast.Attribute | ast.Subscript)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and _target_mentions_name(node, "tier_start")
        ):
            offenders.append(
                f"line {node.lineno}: tier_start is mutated after reservation"
            )
    return offenders


def _contains_name_load(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Name)
        and child.id == name
        and isinstance(child.ctx, ast.Load)
        for child in ast.walk(node)
    )


def _target_names(target: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(target)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    }


def _is_name_load(node: ast.AST | None, name: str) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id == name
        and isinstance(node.ctx, ast.Load)
    )


def _is_tier_start_slice(slice_: ast.AST, *, lower: bool) -> bool:
    if not isinstance(slice_, ast.Slice):
        return False
    endpoint = slice_.lower if lower else slice_.upper
    other_endpoint = slice_.upper if lower else slice_.lower
    return _is_name_load(endpoint, "tier_start") and other_endpoint is None


def _rotation_ring_name(value: ast.expr | None) -> str | None:
    """Return the ring name only for ``ring[start:] + ring[:start]``."""
    if not isinstance(value, ast.BinOp) or not isinstance(value.op, ast.Add):
        return None
    left, right = value.left, value.right
    if not isinstance(left, ast.Subscript) or not isinstance(right, ast.Subscript):
        return None
    if not (
        isinstance(left.value, ast.Name)
        and isinstance(right.value, ast.Name)
        and left.value.id == right.value.id
    ):
        return None
    if not _is_tier_start_slice(left.slice, lower=True):
        return None
    if not _is_tier_start_slice(right.slice, lower=False):
        return None
    return left.value.id


def _is_exact_tier_ring(value: ast.expr | None) -> bool:
    if not isinstance(value, ast.Tuple | ast.List):
        return False
    elements = value.elts
    integer_ring = [
        element.value
        for element in elements
        if isinstance(element, ast.Constant) and type(element.value) is int
    ]
    if integer_ring == [0, 1, 2, 3] and len(integer_ring) == len(elements):
        return True
    return [element.id for element in elements if isinstance(element, ast.Name)] == [
        "TIER_MALFORMED",
        "TIER_EXHAUSTED",
        "TIER_QUEUED",
        "TIER_STALE",
    ]


def _call_uses_loop_name(call: ast.Call, loop_names: set[str]) -> bool:
    return any(
        _contains_name_load(argument, name)
        for argument in (*call.args, *(keyword.value for keyword in call.keywords))
        for name in loop_names
    )


def _tier_order_rotation_offenders(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """Require a real cyclic ``tier_start`` rotation to drive selection.

    Merely accepting or reading ``tier_start`` is not enough: the selected
    candidate loop must iterate an order derived as ``ring[start:] +
    ring[:start]``.  That shape makes the complete four-tier cycle visible to
    the verifier without accepting an unrelated conditional or an imported
    process-local iterator as a lookalike.
    """
    exact_rings: set[str] = set()
    assignments: list[tuple[ast.expr | None, list[ast.expr]]] = []
    for node in _direct_scope_nodes(method):
        value, targets = _assignment_parts(node)
        assignments.append((value, targets))
        if _is_exact_tier_ring(value):
            exact_rings.update(
                target.id for target in targets if isinstance(target, ast.Name)
            )

    rotated_orders: dict[str, str] = {}
    for value, targets in assignments:
        ring_name = _rotation_ring_name(value)
        if ring_name is None or ring_name not in exact_rings:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                rotated_orders[target.id] = ring_name
    if not rotated_orders:
        return [
            "claimable_job_ids does not rotate the exact four-tier order from "
            "tier_start"
        ]

    for node in _direct_scope_nodes(method):
        if not isinstance(node, ast.For | ast.AsyncFor):
            continue
        if not any(_contains_name_load(node.iter, name) for name in rotated_orders):
            continue
        loop_names = _target_names(node.target)
        if any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "get"
            and _call_uses_loop_name(call, loop_names)
            for call in ast.walk(node)
        ):
            return []
    return ["claimable_job_ids does not select candidates in the rotated tier order"]


def _durable_tier_start_offenders(
    *, recovery_source: str, service_source: str
) -> list[str]:
    """The committed singleton reservation is the only tier-start authority."""
    recovery_tree = ast.parse(recovery_source)
    service_tree = ast.parse(service_source)
    recover = _function_named(recovery_tree, "recover_callback_jobs")
    reserve = _function_named(recovery_tree, "reserve_recovery_tier_block")
    service_claimable = _method_named(
        service_tree,
        class_name="CallbackInboxService",
        name="claimable_job_ids",
    )
    offenders: list[str] = []
    if recover is None:
        return ["recover_callback_jobs is missing"]
    if reserve is None:
        offenders.append("reserve_recovery_tier_block is missing")
    if service_claimable is None:
        offenders.append("CallbackInboxService.claimable_job_ids is missing")

    assignments: list[tuple[ast.AST, ast.expr | None, set[int]]] = []
    for node in _direct_scope_nodes(recover):
        value, targets = _assignment_parts(node)
        initial_targets = {
            id(target)
            for target in targets
            if isinstance(target, ast.Name) and target.id == "tier_start"
        }
        if initial_targets:
            assignments.append((node, value, initial_targets))
    if len(assignments) != 1:
        offenders.append(
            f"tier_start has {len(assignments)} direct bindings, expected one"
        )
    elif not (
        isinstance(assignments[0][1], ast.Await)
        and isinstance(assignments[0][1].value, ast.Call)
        and isinstance(assignments[0][1].value.func, ast.Name)
        and assignments[0][1].value.func.id == "reserve_recovery_tier_block"
    ):
        offenders.append(
            "tier_start is not assigned directly from the committed reservation"
        )
    elif assignments:
        offenders.extend(
            _tier_start_store_offenders(recover, initial_targets=assignments[0][2])
        )
    recover_reservations = [
        node
        for node in _direct_scope_nodes(recover)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "reserve_recovery_tier_block"
    ]
    if len(recover_reservations) != 1:
        offenders.append(
            f"recovery has {len(recover_reservations)} tier reservations, expected one"
        )

    claim_calls = [
        node
        for node in _direct_scope_nodes(recover)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "claimable_job_ids"
    ]
    if len(claim_calls) != 1:
        offenders.append(
            f"recovery has {len(claim_calls)} claimable scans, expected one"
        )
    else:
        tier_start = _call_keyword(claim_calls[0], "tier_start")
        if not isinstance(tier_start, ast.Name) or tier_start.id != "tier_start":
            offenders.append("claimable_job_ids does not receive tier_start unchanged")

    if reserve is not None:
        reserve_calls = [
            node
            for node in _direct_scope_nodes(reserve)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "reserve_recovery_tier_block"
        ]
        commits = [
            node
            for node in _direct_scope_nodes(reserve)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "commit"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "session"
        ]
        if len(reserve_calls) != 1 or not any(
            commit.lineno > reserve_calls[0].lineno for commit in commits
        ):
            offenders.append("reservation is not committed before recovery scans")

    if service_claimable is not None:
        keyword_args = [arg.arg for arg in service_claimable.args.kwonlyargs]
        if "tier_start" not in keyword_args:
            offenders.append("claimable_job_ids does not require tier_start")
        elif (
            service_claimable.args.kw_defaults[keyword_args.index("tier_start")]
            is not None
        ):
            offenders.append("claimable_job_ids gives tier_start a local fallback")
        elif not any(
            isinstance(node, ast.Name)
            and node.id == "tier_start"
            and isinstance(node.ctx, ast.Load)
            for node in _direct_scope_nodes(service_claimable)
        ):
            offenders.append("claimable_job_ids ignores tier_start")
        offenders.extend(_tier_order_rotation_offenders(service_claimable))
    return offenders


def _valid_recovery_source(*, after_reservation: str = "") -> str:
    return f"""
async def reserve_recovery_tier_block():
    await service.reserve_recovery_tier_block()
    await session.commit()


async def recover_callback_jobs():
    tier_start = await reserve_recovery_tier_block()
{after_reservation}    candidates = await service.claimable_job_ids(
        now=now,
        limit=limit,
        tier_start=tier_start,
    )
    return candidates
"""


def _rotating_service_source() -> str:
    return """
class CallbackInboxService:
    async def claimable_job_ids(self, *, now, limit, tier_start):
        tier_ring = (0, 1, 2, 3)
        rotated_tiers = tier_ring[tier_start:] + tier_ring[:tier_start]
        by_tier = {}
        ordered = []
        for tier in rotated_tiers:
            ordered.extend(by_tier.get(tier, []))
        return ordered
"""


@pytest.mark.unit
def test_durable_tier_start_guard_allows_the_direct_reservation_passthrough() -> None:
    assert (
        _durable_tier_start_offenders(
            recovery_source=_valid_recovery_source(),
            service_source=_rotating_service_source(),
        )
        == []
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "after_reservation", "expected"),
    (
        (
            "plus_equals",
            "    tier_start += 1\n",
            "tier_start is rebound or deleted after reservation",
        ),
        (
            "rebind_transform",
            "    tier_start = (tier_start + 1) % 4\n",
            "tier_start has 2 direct bindings",
        ),
        (
            "named_expression",
            "    if (tier_start := tier_start + 1):\n        pass\n",
            "tier_start is rebound or deleted after reservation",
        ),
        (
            "delete",
            "    del tier_start\n",
            "tier_start is rebound or deleted after reservation",
        ),
        (
            "for_target",
            "    for tier_start in ():\n        pass\n",
            "tier_start is rebound or deleted after reservation",
        ),
        (
            "import_alias",
            "    import itertools as tier_start\n",
            "tier_start is rebound by an import alias",
        ),
        (
            "subscript_mutation",
            "    tier_start[0] = 1\n",
            "tier_start is mutated after reservation",
        ),
    ),
)
def test_durable_tier_start_guard_rejects_every_post_reservation_mutation(
    label: str, after_reservation: str, expected: str
) -> None:
    offenders = _durable_tier_start_offenders(
        recovery_source=_valid_recovery_source(after_reservation=after_reservation),
        service_source=_rotating_service_source(),
    )
    assert any(expected in offender for offender in offenders), (
        label,
        offenders,
    )


@pytest.mark.unit
def test_rotation_verifiers_reject_an_imported_itertools_cycle_bypass() -> None:
    service_source = """
import itertools


class CallbackInboxService:
    _rotation = itertools.cycle((0, 1, 2, 3))

    async def claimable_job_ids(self, *, now, limit, tier_start):
        if tier_start is None:
            return []
        selected = next(self._rotation)
        by_tier = {}
        ordered = []
        for tier in (selected,):
            ordered.extend(by_tier.get(tier, []))
        return ordered
"""
    state_offenders = _rotation_state_offenders(service_source)
    assert any("itertools.cycle" in offender for offender in state_offenders), (
        state_offenders
    )
    flow_offenders = _durable_tier_start_offenders(
        recovery_source=_valid_recovery_source(),
        service_source=service_source,
    )
    assert any("tier order" in offender for offender in flow_offenders), flow_offenders


@pytest.mark.unit
@pytest.mark.parametrize(
    "service_source",
    (
        """
class CallbackInboxService:
    async def claimable_job_ids(self, *, now, limit, tier_start):
        tier_ring = (0, 1, 2, 3)
        accepted = tier_start
        rotated_tiers = tier_ring[0:] + tier_ring[:0]
        by_tier = {}
        ordered = []
        for tier in rotated_tiers:
            ordered.extend(by_tier.get(tier, []))
        return ordered
""",
        """
class CallbackInboxService:
    async def claimable_job_ids(self, *, now, limit, tier_start):
        tier_ring = (0, 1, 2)
        rotated_tiers = tier_ring[tier_start:] + tier_ring[:tier_start]
        by_tier = {}
        ordered = []
        for tier in rotated_tiers:
            ordered.extend(by_tier.get(tier, []))
        return ordered
""",
    ),
)
def test_durable_tier_start_guard_rejects_acceptance_or_a_nonfour_tier_ring(
    service_source: str,
) -> None:
    offenders = _durable_tier_start_offenders(
        recovery_source=_valid_recovery_source(),
        service_source=service_source,
    )
    assert any(
        "rotate the exact four-tier order" in offender for offender in offenders
    ), offenders


@pytest.mark.unit
def test_rotation_state_guard_rejects_the_custom_ring_bypass() -> None:
    hostile = """
class _Ring:
    def __init__(self):
        self._next = 0

    def reserve(self, width):
        start = self._next
        self._next = (self._next + width) % 4
        return start


module_ring = _Ring()


class CallbackInboxService:
    _rotation_state = _Ring()

    async def claimable_job_ids(self, *, now, limit, tier_start):
        return []


def make_rotation():
    ring = _Ring()

    def next_tier():
        return ring.reserve(1)

    return next_tier
"""
    offenders = _rotation_state_offenders(hostile)
    assert any("CallbackInboxService" in offender for offender in offenders), offenders
    assert any("_rotation_state" in offender for offender in offenders), offenders
    assert any("module_ring" in offender for offender in offenders), offenders
    assert any(
        "closes over mutable _Ring as ring" in offender for offender in offenders
    ), offenders


@pytest.mark.unit
def test_rotation_state_guard_allows_a_harmless_local_cursor_name() -> None:
    harmless = """
import itertools


async def recover_callback_jobs():
    cursor_note = "observability label only"
    local_iterator = itertools.cycle((0,))
    return cursor_note
"""
    assert _rotation_state_offenders(harmless) == []


@pytest.mark.unit
def test_recovery_modules_use_only_a_committed_durable_tier_start() -> None:
    """Durable ordering may live only in the singleton database row."""
    from app.services.order_proposals.callback_inbox import (
        recovery,
        repository,
        service,
    )

    sources = {
        module.__name__: inspect.getsource(module)
        for module in (recovery, repository, service)
    }
    state_offenders = [
        f"{module}: {offender}"
        for module, source in sources.items()
        for offender in _rotation_state_offenders(source)
    ]
    assert not state_offenders, state_offenders
    assert not _durable_tier_start_offenders(
        recovery_source=sources[recovery.__name__],
        service_source=sources[service.__name__],
    )


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
