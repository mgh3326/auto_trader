"""Operational kill-switch contracts and orchestration for ROB-849."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.db import AsyncSessionLocal
from app.services.paper_cohort import contracts
from app.services.paper_cohort.cohort_service import PaperCohortService
from app.services.paper_cohort.kill_switch import PaperCohortKillSwitch
from app.services.paper_validation.contracts import ActorIdentity, ActorRole
from tests.services.paper_cohort.test_cohort_service import (
    _activation,
    _assignment,
    _authoritative_history,
    _registry_rows,
)


def test_kill_request_accepts_only_server_safe_operator_input() -> None:
    request_type = getattr(contracts, "PaperCohortKillRequest", None)

    assert request_type is not None
    assert set(request_type.model_fields) == {
        "cohort_id",
        "idempotency_key",
        "reason_code",
        "reason_text",
    }
    with pytest.raises(ValidationError):
        request_type(
            cohort_id="cohort-1",
            idempotency_key="kill-1",
            reason_code="operator_kill",
            reason_text="stop this paper cohort",
            actor_id="spoofed-operator",
        )


def test_kill_result_exposes_only_stable_fence_and_cleanup_evidence() -> None:
    result_type = getattr(contracts, "PaperCohortKillResult", None)
    cleanup_type = getattr(contracts, "PaperCohortLinkCleanupResult", None)

    assert result_type is not None
    assert cleanup_type is not None
    result = result_type(
        status="fenced",
        fence_id="fence-1",
        cohort_id="cohort-1",
        fenced_at=datetime(2026, 7, 14, tzinfo=UTC),
        replayed=False,
        cleanup_status="pending",
        cleanup_results=(
            cleanup_type(
                link_id=1,
                venue="alpaca",
                status="pending",
                action="cancel",
                reason_code="cancel_pending",
                replayed=False,
            ),
        ),
    )

    assert set(result.model_dump()) == {
        "status",
        "fence_id",
        "cohort_id",
        "fenced_at",
        "replayed",
        "cleanup_status",
        "cleanup_results",
    }
    assert "message" not in cleanup_type.model_fields
    assert "evidence" not in cleanup_type.model_fields


class _ActorProvider:
    def __init__(self, role: ActorRole) -> None:
        self.role = role

    async def resolve(self, caller_id: str) -> ActorIdentity:
        return ActorIdentity(actor_id=caller_id, role=self.role)


class _Transaction:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> None:
        self.events.append("transaction_enter")

    async def __aexit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        del traceback
        self.events.append("rollback" if exc_type or exc else "commit")


class _Session:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> _Session:
        self.events.append("session_enter")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        del exc_type, exc, traceback
        self.events.append("session_exit")

    def begin(self) -> _Transaction:
        return _Transaction(self.events)


class _SessionFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    def __call__(self) -> _Session:
        self.calls += 1
        return _Session(self.events)


def _request() -> contracts.PaperCohortKillRequest:
    return contracts.PaperCohortKillRequest(
        cohort_id="cohort-1",
        idempotency_key="kill-1",
        reason_code="operator_kill",
        reason_text="stop this paper cohort",
    )


def _fence() -> SimpleNamespace:
    return SimpleNamespace(
        fence_id="fence-1",
        cohort_id="cohort-1",
        fenced_at=datetime(2026, 7, 14, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_fence_transaction_commits_before_cleanup_side_effects() -> None:
    events: list[str] = []

    async def cleanup(cohort_id: str):  # noqa: ANN202
        assert cohort_id == "cohort-1"
        assert "commit" in events
        events.append("cleanup")
        return ()

    async def recover(cohort_id: str) -> None:
        assert cohort_id == "cohort-1"
        assert "commit" in events
        events.append("recovery")

    service = PaperCohortKillSwitch(
        _SessionFactory(events),
        actor_role_provider=_ActorProvider(ActorRole.OPERATOR),
        recovery_pass=recover,
        cleanup_pass=cleanup,
        clock=lambda: datetime(2026, 7, 14, tzinfo=UTC),
    )
    service._persist_fence = AsyncMock(return_value=(_fence(), False))  # type: ignore[method-assign]

    result = await service.execute("server-operator", _request())

    assert events.index("commit") < events.index("recovery") < events.index("cleanup")
    assert result.status == "fenced"
    assert result.cleanup_status == "complete"


@pytest.mark.asyncio
async def test_cleanup_failure_cannot_roll_back_committed_fence() -> None:
    events: list[str] = []

    async def cleanup(_cohort_id: str):  # noqa: ANN202
        events.append("cleanup_failed")
        raise RuntimeError("provider secret detail")

    service = PaperCohortKillSwitch(
        _SessionFactory(events),
        actor_role_provider=_ActorProvider(ActorRole.SYSTEM),
        recovery_pass=AsyncMock(),
        cleanup_pass=cleanup,
    )
    service._persist_fence = AsyncMock(return_value=(_fence(), True))  # type: ignore[method-assign]

    result = await service.execute("server-system", _request())

    assert "commit" in events
    assert "rollback" not in events
    assert result.status == "already_fenced"
    assert result.replayed is True
    assert result.cleanup_status == "pending"
    assert result.cleanup_results == ()
    assert "provider secret detail" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_researcher_cannot_fence_and_never_opens_a_transaction() -> None:
    events: list[str] = []
    sessions = _SessionFactory(events)
    service = PaperCohortKillSwitch(
        sessions,
        actor_role_provider=_ActorProvider(ActorRole.RESEARCHER),
        recovery_pass=AsyncMock(),
        cleanup_pass=AsyncMock(),
    )

    with pytest.raises(contracts.PaperCohortError, match="kill_switch_forbidden"):
        await service.execute("server-researcher", _request())

    assert sessions.calls == 0


@pytest.mark.asyncio
async def test_recovery_failure_keeps_fence_and_cleanup_result_pending() -> None:
    events: list[str] = []

    async def recover(_cohort_id: str) -> None:
        events.append("native_lookup_failed")
        raise RuntimeError("Authorization: Bearer secret")

    async def cleanup(_cohort_id: str):  # noqa: ANN202
        events.append("cleanup_known_links")
        return ()

    service = PaperCohortKillSwitch(
        _SessionFactory(events),
        actor_role_provider=_ActorProvider(ActorRole.OPERATOR),
        recovery_pass=recover,
        cleanup_pass=cleanup,
    )
    service._persist_fence = AsyncMock(return_value=(_fence(), False))  # type: ignore[method-assign]

    result = await service.execute("server-operator", _request())

    assert "commit" in events
    assert events[-2:] == ["native_lookup_failed", "cleanup_known_links"]
    assert result.cleanup_status == "pending"
    assert "secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_persisted_reason_and_validation_evidence_are_recursively_redacted() -> (
    None
):
    events: list[str] = []
    service = PaperCohortKillSwitch(
        _SessionFactory(events),
        actor_role_provider=_ActorProvider(ActorRole.OPERATOR),
        recovery_pass=AsyncMock(),
        cleanup_pass=AsyncMock(),
    )
    cohort = SimpleNamespace(
        cohort_id="cohort-1", cohort_hash="1" * 64, assignment_count=1
    )
    assignment = SimpleNamespace(assignment_id="assignment-1", validation_id="v-1")
    service._cohort = AsyncMock(return_value=cohort)  # type: ignore[method-assign]
    service._assignments = AsyncMock(return_value=[assignment])  # type: ignore[method-assign]
    service._validation_evidence = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "validations": [
                {
                    "validation_id": "v-1",
                    "token": "validation-secret",
                    "note": "api_key=validation-secret-2",
                }
            ]
        }
    )
    session = AsyncMock()
    session.scalar.return_value = None
    session.add = lambda row: setattr(session, "added", row)

    await service._persist_fence(
        session,
        actor=ActorIdentity(actor_id="server-operator", role=ActorRole.OPERATOR),
        request=contracts.PaperCohortKillRequest(
            cohort_id="cohort-1",
            idempotency_key="kill-1",
            reason_code="operator_kill",
            reason_text="api_key=reason-secret; Authorization: Bearer auth-secret",
        ),
    )

    persisted = session.added
    assert "reason-secret" not in persisted.reason_text
    assert "auth-secret" not in persisted.reason_text
    assert persisted.validation_evidence["validations"][0]["token"] == "[REDACTED]"
    assert "validation-secret-2" not in str(persisted.validation_evidence)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exact_db_replay_resumes_cleanup_and_changed_payload_conflicts(
    db_session,
) -> None:
    nonce = uuid4().hex
    experiment, run = await _registry_rows(db_session, nonce)
    activation = _activation((_assignment(experiment, run, nonce=nonce),), nonce=nonce)
    await _authoritative_history(db_session, activation, state="paper_active")
    await PaperCohortService(db_session).activate(activation)
    await db_session.commit()

    recovery = AsyncMock()
    cleanup = AsyncMock(return_value=())
    service = PaperCohortKillSwitch(
        AsyncSessionLocal,
        actor_role_provider=_ActorProvider(ActorRole.OPERATOR),
        recovery_pass=recovery,
        cleanup_pass=cleanup,
        clock=lambda: datetime(2026, 7, 14, tzinfo=UTC),
    )
    request = contracts.PaperCohortKillRequest(
        cohort_id=activation.cohort_id,
        idempotency_key=f"kill-{nonce}",
        reason_code="operator_kill",
        reason_text="stop exact cohort",
    )

    first = await service.execute("server-operator", request)
    replay = await service.execute("server-operator", request)

    assert (first.status, first.replayed) == ("fenced", False)
    assert (replay.status, replay.replayed) == ("already_fenced", True)
    assert replay.fence_id == first.fence_id
    assert recovery.await_count == cleanup.await_count == 2

    with pytest.raises(
        contracts.PaperCohortError, match="kill_switch_idempotency_conflict"
    ):
        await service.execute(
            "server-operator",
            request.model_copy(update={"reason_text": "changed request"}),
        )
