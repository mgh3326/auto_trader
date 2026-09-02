"""PostgreSQL ports for the bounded Kiwoom coordination owner.

There is no distributed transaction spanning a Kiwoom HTTP acknowledgement
and PostgreSQL.  The safety boundary is therefore write-ahead, not fictional
atomicity: ``review.order_send_intents`` is committed before broker I/O.  The
post-dispatch write below stores the acknowledged lineage and typed dispatch
evidence in one database transaction.  If every post-ACK write fails, the
pre-send claim still survives and restart discovery classifies it as missing
evidence, blocking replay until authoritative reconciliation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kiwoom_authority_cessation import (
    KiwoomAuthorityAttempt,
    KiwoomAuthorityCessationReceipt,
)
from app.models.kiwoom_coordination_lifecycle import KiwoomCoordinationLifecycle
from app.models.review import OrderSendIntent
from app.services.mock_integration.authority_cessation import (
    AUTHORITY_CESSATION_CONTRACT_VERSION,
    AuthorityAttemptStartedV1,
    AuthorityAttemptTerminalState,
    AuthorityAttemptTerminalV1,
    AuthorityCessationKind,
    AuthorityReleaseAssessment,
    canonical_digest,
    terminal_receipt_digest,
    verify_authority_cessation,
)
from app.services.mock_integration.coordination import (
    DispatchEvidence,
    DispatchEvidenceKind,
    DurableSendClaimAdapter,
    MutationCertainty,
)
from app.services.mock_integration.lineage import LineageEnvelope
from app.services.order_send_intent_service import (
    OrderSendIntentReservation,
    OrderSendIntentService,
)

type AsyncSessionFactory = Callable[[], AsyncSession]

_AUTHORITY_TABLE_COLUMNS = {
    "kiwoom_authority_attempts": (
        "authority_attempt_id",
        "baseline_matching_rows",
        "contract_version",
        "created_at",
        "cycle_id",
        "id",
        "key_count",
        "keyset_digest",
        "lane_id",
        "order_attempt_id",
        "owner_binding_digest",
    ),
    "kiwoom_authority_cessation_receipts": (
        "acquired_key_count",
        "authority_attempt_id",
        "claim_row_id",
        "contract_version",
        "created_at",
        "cycle_id",
        "id",
        "in_flight_unknown",
        "key_count",
        "keyset_digest",
        "kind",
        "lane_id",
        "lock_definite_false",
        "lock_statement_dispatched",
        "observer_pid_absent",
        "order_attempt_id",
        "owner_binding_digest",
        "post_release_matching_rows",
        "receipt_digest",
        "terminal_state",
        "termination_returned_exact_true",
        "unlock_true_count",
    ),
}
_AUTHORITY_TABLE_CONSTRAINTS = {
    "kiwoom_authority_attempts": (
        "ck_kiwoom_authority_attempts_baseline_matching_rows_zero",
        "ck_kiwoom_authority_attempts_contract_version_rob1340_v1",
        "ck_kiwoom_authority_attempts_key_count_positive",
        "ck_kiwoom_authority_attempts_keyset_digest_sha256",
        "ck_kiwoom_authority_attempts_lane_kr_kiwoom_mock",
        "ck_kiwoom_authority_attempts_owner_binding_digest_sha256",
        "uq_kiwoom_authority_attempt_id",
    ),
    "kiwoom_authority_cessation_receipts": (
        "ck_kiwoom_authority_cessation_receipts_acquired_count_bounded",
        "ck_kiwoom_authority_cessation_receipts_cessation_kind",
        "ck_kiwoom_authority_cessation_receipts_contract_rob1340_v1",
        "ck_kiwoom_authority_cessation_receipts_key_count_positive",
        "ck_kiwoom_authority_cessation_receipts_keyset_digest_sha256",
        "ck_kiwoom_authority_cessation_receipts_kind_exact_proof",
        "ck_kiwoom_authority_cessation_receipts_lane_kr_kiwoom_mock",
        "ck_kiwoom_authority_cessation_receipts_owner_digest_sha256",
        "ck_kiwoom_authority_cessation_receipts_receipt_digest_sha256",
        "ck_kiwoom_authority_cessation_receipts_terminal_state",
        "ck_kiwoom_authority_cessation_receipts_unlock_count_bounded",
        "uq_kiwoom_authority_receipt_attempt",
        "uq_kiwoom_authority_receipt_digest",
    ),
}


def _authority_schema_projection(
    *,
    columns: dict[str, tuple[str, ...]],
    constraints: dict[str, tuple[str, ...]],
    privileges: dict[str, dict[str, bool]],
) -> dict[str, object]:
    return {
        "contract_version": AUTHORITY_CESSATION_CONTRACT_VERSION,
        "tables": [
            {
                "name": table,
                "columns": list(columns[table]),
                "constraints": list(constraints[table]),
                "privileges": privileges[table],
            }
            for table in sorted(_AUTHORITY_TABLE_COLUMNS)
        ],
    }


_EXPECTED_AUTHORITY_SCHEMA_PROJECTION = _authority_schema_projection(
    columns=_AUTHORITY_TABLE_COLUMNS,
    constraints=_AUTHORITY_TABLE_CONSTRAINTS,
    privileges={
        table: {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
            "TRUNCATE": False,
        }
        for table in _AUTHORITY_TABLE_COLUMNS
    },
)
AUTHORITY_SCHEMA_CONTRACT_DIGEST = canonical_digest(
    _EXPECTED_AUTHORITY_SCHEMA_PROJECTION,
    domain="rob1340-authority-schema-v1",
)


class KiwoomCoordinationStoreConflict(RuntimeError):
    """A durable row disagrees with the immutable lineage being persisted."""


class KiwoomRestartClaimState(StrEnum):
    """Restart classification derived only from committed database rows."""

    EVIDENCE_MISSING = "evidence_missing"
    UNCERTAIN = "uncertain"
    DEFINITIVE_TRACKED = "definitive_tracked"


@dataclass(frozen=True, slots=True)
class RediscoveredKiwoomClaim:
    """One unreleased binary claim reconstructed after process restart."""

    row_id: int
    claim_account_scope: str
    idempotency_key: str
    side: str | None
    state: KiwoomRestartClaimState
    dispatch_kind: DispatchEvidenceKind | None
    certainty: MutationCertainty | None
    broker_order_id: str | None
    ack_envelope_recorded: bool

    @property
    def blocks_account(self) -> bool:
        """Missing or explicitly uncertain evidence fail-closes the account."""

        return self.state in {
            KiwoomRestartClaimState.EVIDENCE_MISSING,
            KiwoomRestartClaimState.UNCERTAIN,
        }


def _envelope_parts(
    envelope: LineageEnvelope,
) -> tuple[Any, Any, dict[str, Any]]:
    if type(envelope) is not LineageEnvelope:
        raise KiwoomCoordinationStoreConflict("lineage_envelope_type_rejected")
    execution_plan = envelope.execution_plan
    order_attempt = envelope.order_attempt
    if execution_plan is None or order_attempt is None:
        raise KiwoomCoordinationStoreConflict("attempt_lineage_required")
    return execution_plan, order_attempt, envelope.model_dump(mode="json")


class KiwoomOrderSendIntentPort:
    """Fresh-session adapter over the existing binary reservation service."""

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def reserve(
        self,
        *,
        account_scope: str,
        idempotency_key: str,
        symbol: str | None = None,
        side: str | None = None,
        conflicting_key_sides: tuple[tuple[str, str], ...] = (),
    ) -> int:
        async with self._session_factory() as session:
            return await OrderSendIntentService(session).reserve(
                account_scope=account_scope,
                idempotency_key=idempotency_key,
                symbol=symbol,
                side=side,
                conflicting_key_sides=conflicting_key_sides,
            )

    async def list_reservations(
        self, *, account_scope: str
    ) -> Sequence[OrderSendIntentReservation]:
        async with self._session_factory() as session:
            return await OrderSendIntentService(session).list_reservations(
                account_scope=account_scope
            )

    async def release_if_matches(
        self,
        *,
        account_scope: str,
        row_id: int,
        idempotency_key: str,
        side: str | None,
    ) -> int:
        async with self._session_factory() as session:
            return await OrderSendIntentService(session).release_if_matches(
                account_scope=account_scope,
                row_id=row_id,
                idempotency_key=idempotency_key,
                side=side,
            )


class KiwoomCoordinationStore:
    """Implements lineage, dispatch-evidence, uncertainty, and restart reads."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        lane_id: str,
        physical_account_id: str,
        claim_account_scope: str,
    ) -> None:
        if lane_id != "kr.kiwoom.mock":
            raise KiwoomCoordinationStoreConflict("kiwoom_lane_rejected")
        if not physical_account_id.strip() or not claim_account_scope.strip():
            raise KiwoomCoordinationStoreConflict("kiwoom_account_scope_rejected")
        self._session_factory = session_factory
        self._lane_id = lane_id
        self._physical_account_id = physical_account_id
        self._claim_account_scope = claim_account_scope

    async def assert_ready(self) -> None:
        """Prove the immutable evidence schema and effective app privileges.

        This is a read-only catalog projection on a fresh session.  An INSERT
        probe would itself become a fake authority attempt, so none is used.
        """

        try:
            async with self._session_factory() as session:
                column_rows = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT c.relname AS table_name, a.attname AS column_name
                            FROM pg_catalog.pg_class AS c
                            JOIN pg_catalog.pg_namespace AS n
                              ON n.oid = c.relnamespace
                            JOIN pg_catalog.pg_attribute AS a
                              ON a.attrelid = c.oid
                            WHERE n.nspname = 'review'
                              AND c.relkind IN ('r', 'p')
                              AND c.relname IN (
                                'kiwoom_authority_attempts',
                                'kiwoom_authority_cessation_receipts'
                              )
                              AND a.attnum > 0
                              AND NOT a.attisdropped
                            ORDER BY c.relname, a.attname
                            """
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                constraint_rows = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT c.relname AS table_name,
                                   con.conname AS constraint_name
                            FROM pg_catalog.pg_constraint AS con
                            JOIN pg_catalog.pg_class AS c
                              ON c.oid = con.conrelid
                            JOIN pg_catalog.pg_namespace AS n
                              ON n.oid = c.relnamespace
                            WHERE n.nspname = 'review'
                              AND c.relname IN (
                                'kiwoom_authority_attempts',
                                'kiwoom_authority_cessation_receipts'
                              )
                            ORDER BY c.relname, con.conname
                            """
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                privilege_rows = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT c.relname AS table_name,
                                   has_table_privilege(
                                     current_user, c.oid, 'SELECT'
                                   ) AS can_select,
                                   has_table_privilege(
                                     current_user, c.oid, 'INSERT'
                                   ) AS can_insert,
                                   has_table_privilege(
                                     current_user, c.oid, 'UPDATE'
                                   ) AS can_update,
                                   has_table_privilege(
                                     current_user, c.oid, 'DELETE'
                                   ) AS can_delete,
                                   has_table_privilege(
                                     current_user, c.oid, 'TRUNCATE'
                                   ) AS can_truncate
                            FROM pg_catalog.pg_class AS c
                            JOIN pg_catalog.pg_namespace AS n
                              ON n.oid = c.relnamespace
                            WHERE n.nspname = 'review'
                              AND c.relkind IN ('r', 'p')
                              AND c.relname IN (
                                'kiwoom_authority_attempts',
                                'kiwoom_authority_cessation_receipts'
                              )
                            ORDER BY c.relname
                            """
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
        except Exception as exc:
            raise KiwoomCoordinationStoreConflict(
                "authority_schema_preflight_unavailable"
            ) from exc

        columns: dict[str, tuple[str, ...]] = {}
        constraints: dict[str, tuple[str, ...]] = {}
        privileges: dict[str, dict[str, bool]] = {}
        for table in _AUTHORITY_TABLE_COLUMNS:
            columns[table] = tuple(
                sorted(
                    str(row["column_name"])
                    for row in column_rows
                    if row["table_name"] == table
                )
            )
            required_constraints = set(_AUTHORITY_TABLE_CONSTRAINTS[table])
            constraints[table] = tuple(
                sorted(
                    str(row["constraint_name"])
                    for row in constraint_rows
                    if row["table_name"] == table
                    and row["constraint_name"] in required_constraints
                )
            )
            privilege = next(
                (row for row in privilege_rows if row["table_name"] == table), None
            )
            if privilege is None:
                privileges[table] = {}
            else:
                privileges[table] = {
                    "SELECT": privilege["can_select"] is True,
                    "INSERT": privilege["can_insert"] is True,
                    "UPDATE": privilege["can_update"] is True,
                    "DELETE": privilege["can_delete"] is True,
                    "TRUNCATE": privilege["can_truncate"] is True,
                }

        actual = _authority_schema_projection(
            columns=columns, constraints=constraints, privileges=privileges
        )
        digest = canonical_digest(actual, domain="rob1340-authority-schema-v1")
        if digest != AUTHORITY_SCHEMA_CONTRACT_DIGEST:
            raise KiwoomCoordinationStoreConflict("authority_schema_contract_mismatch")

    @staticmethod
    def _validate_started(started: AuthorityAttemptStartedV1) -> None:
        if (
            type(started) is not AuthorityAttemptStartedV1
            or started.contract_version != AUTHORITY_CESSATION_CONTRACT_VERSION
            or started.lane_id != "kr.kiwoom.mock"
            or not started.authority_attempt_id.strip()
            or not started.cycle_id.strip()
            or not started.order_attempt_id.strip()
            or len(started.owner_binding_digest) != 64
            or len(started.keyset_digest) != 64
            or started.key_count <= 0
            or started.baseline_matching_rows != 0
        ):
            raise KiwoomCoordinationStoreConflict("authority_attempt_shape_rejected")

    @staticmethod
    def _started_matches(
        row: KiwoomAuthorityAttempt, started: AuthorityAttemptStartedV1
    ) -> bool:
        return (
            row.authority_attempt_id == started.authority_attempt_id
            and row.contract_version == started.contract_version
            and row.lane_id == started.lane_id
            and row.cycle_id == started.cycle_id
            and row.order_attempt_id == started.order_attempt_id
            and row.owner_binding_digest == started.owner_binding_digest
            and row.keyset_digest == started.keyset_digest
            and row.key_count == started.key_count
            and row.baseline_matching_rows == started.baseline_matching_rows
        )

    async def _read_started(
        self, authority_attempt_id: str
    ) -> KiwoomAuthorityAttempt | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(KiwoomAuthorityAttempt).where(
                    KiwoomAuthorityAttempt.authority_attempt_id == authority_attempt_id
                )
            )

    async def record_started(
        self, started: AuthorityAttemptStartedV1, /
    ) -> AuthorityAttemptStartedV1:
        """Commit and independently read back a start before lock dispatch."""

        self._validate_started(started)
        existing = await self._read_started(started.authority_attempt_id)
        if existing is not None:
            if not self._started_matches(existing, started):
                raise KiwoomCoordinationStoreConflict(
                    "authority_attempt_immutable_conflict"
                )
            return started

        async with self._session_factory() as session:
            session.add(
                KiwoomAuthorityAttempt(
                    authority_attempt_id=started.authority_attempt_id,
                    contract_version=started.contract_version,
                    lane_id=started.lane_id,
                    cycle_id=started.cycle_id,
                    order_attempt_id=started.order_attempt_id,
                    owner_binding_digest=started.owner_binding_digest,
                    keyset_digest=started.keyset_digest,
                    key_count=started.key_count,
                    baseline_matching_rows=started.baseline_matching_rows,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

        readback = await self._read_started(started.authority_attempt_id)
        if readback is None or not self._started_matches(readback, started):
            raise KiwoomCoordinationStoreConflict(
                "authority_attempt_commit_readback_failed"
            )
        return started

    @staticmethod
    def _terminal_from_row(
        row: KiwoomAuthorityCessationReceipt,
    ) -> AuthorityAttemptTerminalV1:
        return AuthorityAttemptTerminalV1(
            authority_attempt_id=row.authority_attempt_id,
            contract_version=row.contract_version,
            lane_id=row.lane_id,
            cycle_id=row.cycle_id,
            order_attempt_id=row.order_attempt_id,
            claim_row_id=row.claim_row_id,
            owner_binding_digest=row.owner_binding_digest,
            keyset_digest=row.keyset_digest,
            key_count=row.key_count,
            terminal_state=AuthorityAttemptTerminalState(row.terminal_state),
            kind=AuthorityCessationKind(row.kind),
            lock_statement_dispatched=row.lock_statement_dispatched,
            lock_definite_false=row.lock_definite_false,
            acquired_key_count=row.acquired_key_count,
            in_flight_unknown=row.in_flight_unknown,
            unlock_true_count=row.unlock_true_count,
            post_release_matching_rows=row.post_release_matching_rows,
            termination_returned_exact_true=row.termination_returned_exact_true,
            observer_pid_absent=row.observer_pid_absent,
            receipt_digest=row.receipt_digest,
            receipt_id=row.id,
            committed=True,
        )

    async def _read_terminal(
        self, authority_attempt_id: str
    ) -> AuthorityAttemptTerminalV1 | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KiwoomAuthorityCessationReceipt).where(
                    KiwoomAuthorityCessationReceipt.authority_attempt_id
                    == authority_attempt_id
                )
            )
            return None if row is None else self._terminal_from_row(row)

    async def record_terminal(
        self, terminal: AuthorityAttemptTerminalV1, /
    ) -> AuthorityAttemptTerminalV1:
        """Commit/read back the sole terminal row; no UPDATE fallback exists."""

        if (
            type(terminal) is not AuthorityAttemptTerminalV1
            or terminal.committed
            or terminal.receipt_id is not None
            or terminal.contract_version != AUTHORITY_CESSATION_CONTRACT_VERSION
            or terminal.lane_id != "kr.kiwoom.mock"
            or terminal.receipt_digest != terminal_receipt_digest(terminal)
        ):
            raise KiwoomCoordinationStoreConflict("authority_terminal_shape_rejected")
        started = await self._read_started(terminal.authority_attempt_id)
        if started is None or not self._started_matches(
            started,
            AuthorityAttemptStartedV1(
                authority_attempt_id=terminal.authority_attempt_id,
                contract_version=terminal.contract_version,
                lane_id=terminal.lane_id,
                cycle_id=terminal.cycle_id,
                order_attempt_id=terminal.order_attempt_id,
                owner_binding_digest=terminal.owner_binding_digest,
                keyset_digest=terminal.keyset_digest,
                key_count=terminal.key_count,
                baseline_matching_rows=0,
            ),
        ):
            raise KiwoomCoordinationStoreConflict(
                "authority_terminal_attempt_binding_mismatch"
            )

        existing = await self._read_terminal(terminal.authority_attempt_id)
        if existing is not None:
            expected = replace(existing, receipt_id=None, committed=False)
            if expected != terminal:
                raise KiwoomCoordinationStoreConflict(
                    "authority_terminal_immutable_conflict"
                )
            return existing

        async with self._session_factory() as session:
            session.add(
                KiwoomAuthorityCessationReceipt(
                    authority_attempt_id=terminal.authority_attempt_id,
                    contract_version=terminal.contract_version,
                    lane_id=terminal.lane_id,
                    cycle_id=terminal.cycle_id,
                    order_attempt_id=terminal.order_attempt_id,
                    claim_row_id=terminal.claim_row_id,
                    owner_binding_digest=terminal.owner_binding_digest,
                    keyset_digest=terminal.keyset_digest,
                    key_count=terminal.key_count,
                    terminal_state=terminal.terminal_state.value,
                    kind=terminal.kind.value,
                    lock_statement_dispatched=terminal.lock_statement_dispatched,
                    lock_definite_false=terminal.lock_definite_false,
                    acquired_key_count=terminal.acquired_key_count,
                    in_flight_unknown=terminal.in_flight_unknown,
                    unlock_true_count=terminal.unlock_true_count,
                    post_release_matching_rows=(terminal.post_release_matching_rows),
                    termination_returned_exact_true=(
                        terminal.termination_returned_exact_true
                    ),
                    observer_pid_absent=terminal.observer_pid_absent,
                    receipt_digest=terminal.receipt_digest,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

        readback = await self._read_terminal(terminal.authority_attempt_id)
        if (
            readback is None
            or replace(readback, receipt_id=None, committed=False) != terminal
        ):
            raise KiwoomCoordinationStoreConflict(
                "authority_terminal_commit_readback_failed"
            )
        return readback

    @staticmethod
    def _started_from_row(row: KiwoomAuthorityAttempt) -> AuthorityAttemptStartedV1:
        return AuthorityAttemptStartedV1(
            authority_attempt_id=row.authority_attempt_id,
            contract_version=row.contract_version,
            lane_id=row.lane_id,
            cycle_id=row.cycle_id,
            order_attempt_id=row.order_attempt_id,
            owner_binding_digest=row.owner_binding_digest,
            keyset_digest=row.keyset_digest,
            key_count=row.key_count,
            baseline_matching_rows=row.baseline_matching_rows,
        )

    async def release_assessment_for_cycle(
        self, *, cycle_id: str
    ) -> AuthorityReleaseAssessment:
        """Use only this cycle's durable journal; old crash orphans stay local."""

        async with self._session_factory() as session:
            attempt_rows = tuple(
                (
                    await session.scalars(
                        select(KiwoomAuthorityAttempt)
                        .where(KiwoomAuthorityAttempt.cycle_id == cycle_id)
                        .order_by(KiwoomAuthorityAttempt.id)
                    )
                ).all()
            )
            attempt_ids = tuple(row.authority_attempt_id for row in attempt_rows)
            terminal_filter = KiwoomAuthorityCessationReceipt.cycle_id == cycle_id
            if attempt_ids:
                terminal_filter = or_(
                    terminal_filter,
                    KiwoomAuthorityCessationReceipt.authority_attempt_id.in_(
                        attempt_ids
                    ),
                )
            terminal_rows = tuple(
                (
                    await session.scalars(
                        select(KiwoomAuthorityCessationReceipt)
                        .where(terminal_filter)
                        .order_by(KiwoomAuthorityCessationReceipt.id)
                    )
                ).all()
            )

        # Imported lazily: coordination owns the process-local capabilities and
        # imports this store's protocol types at module import time.
        from app.services.mock_integration.coordination import (
            authority_hold_history,
            unreleased_authority_holds_for_cycle,
        )

        history = tuple(
            hold for hold in authority_hold_history() if hold.cycle_id == cycle_id
        )
        active = unreleased_authority_holds_for_cycle(cycle_id)
        enumeration_complete = all(
            hold.authority_attempt_id is not None for hold in history
        )
        history_ids = tuple(
            hold.authority_attempt_id
            for hold in history
            if hold.authority_attempt_id is not None
        )
        active_ids = tuple(
            hold.authority_attempt_id or f"unbound-hold:{hold.hold_id}"
            for hold in active
        )
        return verify_authority_cessation(
            cycle_id=cycle_id,
            attempts=tuple(self._started_from_row(row) for row in attempt_rows),
            terminals=tuple(self._terminal_from_row(row) for row in terminal_rows),
            enumeration_complete=enumeration_complete,
            history_hold_attempt_ids=history_ids,
            active_hold_attempt_ids=active_ids,
        )

    def _validate_plan(self, execution_plan: Any) -> None:
        if (
            execution_plan.lane_id != self._lane_id
            or execution_plan.broker != "kiwoom"
            or execution_plan.account_profile != "mock"
            or execution_plan.account_mode != "mock"
        ):
            raise KiwoomCoordinationStoreConflict("kiwoom_lineage_lane_mismatch")

    def _validate_existing(
        self,
        row: KiwoomCoordinationLifecycle,
        *,
        envelope: LineageEnvelope,
    ) -> tuple[Any, Any, dict[str, Any]]:
        execution_plan, order_attempt, payload = _envelope_parts(envelope)
        self._validate_plan(execution_plan)
        if (
            row.lane_id != self._lane_id
            or row.physical_account_id != self._physical_account_id
            or row.claim_account_scope != self._claim_account_scope
            or row.decision_intent_id != envelope.decision_intent.decision_intent_id
            or row.execution_plan_id != execution_plan.execution_plan_id
            or row.order_attempt_id != order_attempt.order_attempt_id
            or row.idempotency_key != order_attempt.idempotency_key
        ):
            raise KiwoomCoordinationStoreConflict("kiwoom_lineage_conflict")
        return execution_plan, order_attempt, payload

    async def persist(self, envelope: LineageEnvelope, /) -> None:
        """Commit the pre-send lineage or its later immutable ACK attachment."""

        execution_plan, order_attempt, payload = _envelope_parts(envelope)
        self._validate_plan(execution_plan)
        broker_order_id = order_attempt.broker_order_id
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KiwoomCoordinationLifecycle)
                .where(
                    KiwoomCoordinationLifecycle.claim_account_scope
                    == self._claim_account_scope,
                    KiwoomCoordinationLifecycle.idempotency_key
                    == order_attempt.idempotency_key,
                )
                .with_for_update()
            )
            if row is None:
                if broker_order_id is not None:
                    raise KiwoomCoordinationStoreConflict(
                        "pre_send_lineage_missing_before_ack"
                    )
                session.add(
                    KiwoomCoordinationLifecycle(
                        lane_id=self._lane_id,
                        physical_account_id=self._physical_account_id,
                        claim_account_scope=self._claim_account_scope,
                        decision_intent_id=envelope.decision_intent.decision_intent_id,
                        execution_plan_id=execution_plan.execution_plan_id,
                        order_attempt_id=order_attempt.order_attempt_id,
                        idempotency_key=order_attempt.idempotency_key,
                        initial_envelope=payload,
                    )
                )
            else:
                _, _, payload = self._validate_existing(row, envelope=envelope)
                if broker_order_id is None:
                    if row.initial_envelope != payload:
                        raise KiwoomCoordinationStoreConflict(
                            "initial_lineage_immutable"
                        )
                elif row.ack_envelope is None:
                    if row.broker_order_id not in (None, broker_order_id):
                        raise KiwoomCoordinationStoreConflict(
                            "broker_order_id_conflict"
                        )
                    row.ack_envelope = payload
                    row.broker_order_id = broker_order_id
                elif (
                    row.ack_envelope != payload
                    or row.broker_order_id != broker_order_id
                ):
                    raise KiwoomCoordinationStoreConflict("ack_lineage_immutable")
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise KiwoomCoordinationStoreConflict(
                    "kiwoom_lineage_conflict"
                ) from exc

    async def persist_dispatch_evidence(self, evidence: DispatchEvidence, /) -> None:
        """Atomically persist the ACK envelope and typed dispatch evidence."""

        if type(evidence) is not DispatchEvidence:
            raise KiwoomCoordinationStoreConflict("dispatch_evidence_type_rejected")
        if (
            type(evidence.kind) is not DispatchEvidenceKind
            or type(evidence.certainty) is not MutationCertainty
            or type(evidence.claim_row_id) is not int
            or any(
                type(value) is not bool
                for value in (
                    evidence.callback_failed,
                    evidence.ack_attachment_failed,
                    evidence.outer_cancellation_requested,
                )
            )
            or (
                evidence.broker_order_id is not None
                and (
                    type(evidence.broker_order_id) is not str
                    or not evidence.broker_order_id.strip()
                )
            )
        ):
            raise KiwoomCoordinationStoreConflict("dispatch_evidence_shape_rejected")
        execution_plan, order_attempt, payload = _envelope_parts(evidence.envelope)
        self._validate_plan(execution_plan)
        if (
            evidence.claim_account_scope != self._claim_account_scope
            or evidence.decision_intent_id
            != evidence.envelope.decision_intent.decision_intent_id
            or evidence.execution_plan_id != execution_plan.execution_plan_id
            or evidence.order_attempt_id != order_attempt.order_attempt_id
            or evidence.idempotency_key != order_attempt.idempotency_key
        ):
            raise KiwoomCoordinationStoreConflict("dispatch_lineage_mismatch")

        async with self._session_factory() as session:
            row = await session.scalar(
                select(KiwoomCoordinationLifecycle)
                .where(
                    KiwoomCoordinationLifecycle.claim_account_scope
                    == self._claim_account_scope,
                    KiwoomCoordinationLifecycle.idempotency_key
                    == evidence.idempotency_key,
                )
                .with_for_update()
            )
            if row is None:
                raise KiwoomCoordinationStoreConflict("pre_send_lineage_missing")
            self._validate_existing(row, envelope=evidence.envelope)
            claim = await session.scalar(
                select(OrderSendIntent).where(
                    OrderSendIntent.id == evidence.claim_row_id,
                    OrderSendIntent.account_scope == self._claim_account_scope,
                    OrderSendIntent.idempotency_key == evidence.idempotency_key,
                )
            )
            if claim is None or (
                claim.side is not None
                and claim.side != evidence.envelope.decision_intent.side
            ):
                raise KiwoomCoordinationStoreConflict("dispatch_claim_mismatch")

            projection = (
                payload,
                evidence.kind.value,
                evidence.certainty.value,
                evidence.claim_row_id,
                evidence.callback_failed,
                evidence.ack_attachment_failed,
                evidence.outer_cancellation_requested,
                evidence.broker_order_id,
            )
            existing_projection = (
                row.dispatch_envelope,
                row.dispatch_kind,
                row.mutation_certainty,
                row.claim_row_id,
                row.callback_failed,
                row.ack_attachment_failed,
                row.outer_cancellation_requested,
                row.broker_order_id,
            )
            if row.dispatch_kind is not None:
                if existing_projection != projection:
                    raise KiwoomCoordinationStoreConflict("dispatch_evidence_immutable")
                return

            if (
                row.broker_order_id is not None
                and evidence.broker_order_id is not None
                and row.broker_order_id != evidence.broker_order_id
            ):
                raise KiwoomCoordinationStoreConflict("broker_order_id_conflict")
            row.dispatch_envelope = payload
            row.dispatch_kind = evidence.kind.value
            row.mutation_certainty = evidence.certainty.value
            row.claim_row_id = evidence.claim_row_id
            row.callback_failed = evidence.callback_failed
            row.ack_attachment_failed = evidence.ack_attachment_failed
            row.outer_cancellation_requested = evidence.outer_cancellation_requested
            if evidence.broker_order_id is not None:
                row.broker_order_id = evidence.broker_order_id
            if order_attempt.broker_order_id is not None:
                if row.broker_order_id != order_attempt.broker_order_id:
                    raise KiwoomCoordinationStoreConflict("broker_order_id_conflict")
                if row.ack_envelope is None:
                    row.ack_envelope = payload
                elif row.ack_envelope != payload:
                    raise KiwoomCoordinationStoreConflict("ack_lineage_immutable")
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise KiwoomCoordinationStoreConflict(
                    "dispatch_evidence_conflict"
                ) from exc

    async def rediscover_unreleased_claims(
        self,
    ) -> tuple[RediscoveredKiwoomClaim, ...]:
        """Left-join every surviving claim to evidence in a fresh session."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(OrderSendIntent, KiwoomCoordinationLifecycle)
                .outerjoin(
                    KiwoomCoordinationLifecycle,
                    and_(
                        KiwoomCoordinationLifecycle.claim_account_scope
                        == OrderSendIntent.account_scope,
                        KiwoomCoordinationLifecycle.idempotency_key
                        == OrderSendIntent.idempotency_key,
                    ),
                )
                .where(OrderSendIntent.account_scope == self._claim_account_scope)
                .order_by(OrderSendIntent.id)
            )
            discovered: list[RediscoveredKiwoomClaim] = []
            for claim, lifecycle in result.all():
                kind = (
                    None
                    if lifecycle is None or lifecycle.dispatch_kind is None
                    else DispatchEvidenceKind(lifecycle.dispatch_kind)
                )
                certainty = (
                    None
                    if lifecycle is None or lifecycle.mutation_certainty is None
                    else MutationCertainty(lifecycle.mutation_certainty)
                )
                if kind is None or certainty is None:
                    state = KiwoomRestartClaimState.EVIDENCE_MISSING
                elif certainty is MutationCertainty.UNCERTAIN or kind in {
                    DispatchEvidenceKind.LANE_REPORTED_UNCERTAIN,
                    DispatchEvidenceKind.CALLBACK_FAILED,
                    DispatchEvidenceKind.ACK_ATTACHMENT_FAILED,
                }:
                    state = KiwoomRestartClaimState.UNCERTAIN
                else:
                    state = KiwoomRestartClaimState.DEFINITIVE_TRACKED
                discovered.append(
                    RediscoveredKiwoomClaim(
                        row_id=claim.id,
                        claim_account_scope=claim.account_scope,
                        idempotency_key=claim.idempotency_key,
                        side=claim.side,
                        state=state,
                        dispatch_kind=kind,
                        certainty=certainty,
                        broker_order_id=(
                            None if lifecycle is None else lifecycle.broker_order_id
                        ),
                        ack_envelope_recorded=(
                            lifecycle is not None and lifecycle.ack_envelope is not None
                        ),
                    )
                )
            return tuple(discovered)

    async def has_unresolved_account_uncertainty(
        self, *, claim_account_scope: str
    ) -> bool:
        """Rediscover durable claims under the lease and fail closed on gaps."""

        if claim_account_scope != self._claim_account_scope:
            raise KiwoomCoordinationStoreConflict("claim_account_scope_mismatch")
        return any(
            claim.blocks_account for claim in await self.rediscover_unreleased_claims()
        )


class KiwoomDurableSendClaimAdapter(DurableSendClaimAdapter):
    """J2B claims plus the lane-owned restart rediscovery operation."""

    __slots__ = ("_store",)

    def __init__(
        self,
        intents: KiwoomOrderSendIntentPort,
        *,
        store: KiwoomCoordinationStore,
    ) -> None:
        super().__init__(intents)
        self._store = store

    async def rediscover_unreleased_claims(
        self,
    ) -> tuple[RediscoveredKiwoomClaim, ...]:
        """Recovery owner entrypoint used after constructing fresh ports."""

        return await self._store.rediscover_unreleased_claims()


__all__ = [
    "KiwoomCoordinationStore",
    "KiwoomCoordinationStoreConflict",
    "KiwoomDurableSendClaimAdapter",
    "KiwoomOrderSendIntentPort",
    "KiwoomRestartClaimState",
    "RediscoveredKiwoomClaim",
]
