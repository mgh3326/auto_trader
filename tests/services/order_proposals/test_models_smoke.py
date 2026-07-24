import ast
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models import order_proposals as models
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.order_proposals.state_machine import RUNG_STATES
from tests import _schema_bootstrap as schema_bootstrap

_REPO = Path(__file__).resolve().parents[3]
_DISPATCH_MIGRATION = _REPO / "alembic/versions/20260723_approval_dispatch_ledger.py"


def _migration_application_dml(source: str) -> list[tuple[str, str]]:
    """Find application-table DML only inside migration operation calls."""
    tree = ast.parse(source)
    findings: list[tuple[str, str]] = []
    pattern = re.compile(
        r"\b(insert\s+into|update|delete\s+from)\s+([a-z_][a-z0-9_$.\"']*)",
        re.IGNORECASE,
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
            and node.func.attr == "bulk_insert"
        ):
            findings.append(("bulk_insert", "unknown"))
            continue
        if (
            not isinstance(node.func.value, ast.Name)
            or node.func.value.id != "op"
            or node.func.attr != "execute"
            or not node.args
        ):
            continue
        sql_parts = [
            value.value
            for value in ast.walk(node.args[0])
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        ]
        sql = "".join(sql_parts)
        for match in pattern.finditer(sql):
            verb = " ".join(match.group(1).lower().split())
            relation = match.group(2).strip("\"'").lower()
            if relation.rsplit(".", 1)[-1] != "alembic_version":
                findings.append((verb, relation))
    return findings


@pytest.mark.unit
def test_tables_in_review_schema():
    assert OrderProposal.__table__.schema == "review"
    assert OrderProposalRung.__table__.schema == "review"
    assert OrderProposal.__tablename__ == "order_proposals"
    assert OrderProposalRung.__tablename__ == "order_proposal_rungs"


def test_order_proposal_has_group_level_loss_cut_binding_columns():
    columns = OrderProposal.__table__.columns
    assert columns["exit_intent"].nullable
    assert columns["exit_reason"].nullable
    assert columns["retrospective_id"].nullable
    assert columns["approval_issue_id"].nullable
    assert "exit_intent" not in OrderProposalRung.__table__.columns


@pytest.mark.unit
def test_order_proposal_has_action_columns():
    columns = OrderProposal.__table__.columns
    assert columns["action"].nullable
    assert columns["target_broker_order_id"].nullable
    check = next(
        c
        for c in OrderProposal.__table__.constraints
        if getattr(c, "name", None) == "ck_order_proposals_order_proposals_action"
    )
    assert "action IS NULL" in str(check.sqltext)
    for action in ("place", "replace", "cancel"):
        assert f"'{action}'" in str(check.sqltext)


@pytest.mark.unit
def test_approval_dispatch_models_are_durable_and_attempt_scoped():
    proposal = OrderProposal.__table__
    attempt = models.OrderProposalApprovalDispatchAttempt.__table__

    assert {
        "approval_dispatch_state",
        "approval_dispatch_attempt_id",
        "approval_dispatch_attempted_at",
        "approval_dispatch_failure_code",
        "approval_dispatch_payload_chars",
        "approval_dispatch_card_kind",
        "approval_dispatch_membership_revision",
        "approval_dispatch_membership_digest",
        "approval_dispatch_published_at",
    } <= set(proposal.columns.keys())
    assert attempt.schema == "review"
    assert attempt.name == "order_proposal_approval_dispatch_attempts"
    assert {
        "attempt_id",
        "proposal_pk",
        "state",
        "attempted_at",
        "completed_at",
        "payload_chars",
        "context_message_count",
        "message_id",
        "status_code",
        "telegram_error_code",
        "error_classification",
        "failure_code",
        "card_kind",
        "membership_revision",
        "membership_digest",
    } <= set(attempt.columns.keys())
    assert "telegram_description" not in attempt.columns


@pytest.mark.unit
def test_approval_dispatch_model_constraints_are_closed_and_non_nullable() -> None:
    proposal = OrderProposal.__table__
    attempt = models.OrderProposalApprovalDispatchAttempt.__table__
    batch = models.OrderProposalApprovalBatch.__table__
    expected_states = set(schema_bootstrap._APPROVAL_DISPATCH_STATES)
    expected_card_kinds = set(schema_bootstrap._APPROVAL_PROPOSAL_CARD_KINDS)

    def check_values(table, column: str) -> set[str]:
        constraint = next(
            constraint
            for constraint in table.constraints
            if column in str(getattr(constraint, "sqltext", ""))
        )
        return set(re.findall(r"'([^']+)'", str(constraint.sqltext)))

    assert check_values(proposal, "approval_dispatch_state") == expected_states
    assert check_values(proposal, "approval_dispatch_card_kind") == expected_card_kinds
    assert check_values(batch, "approval_dispatch_state") == expected_states
    assert check_values(attempt, "state") == expected_states
    assert check_values(attempt, "card_kind") == expected_card_kinds
    assert all(
        not attempt.columns[column].nullable
        for column in schema_bootstrap._APPROVAL_ATTEMPT_NOT_NULL_COLUMNS
    )


@pytest.mark.unit
def test_approval_dispatch_migration_is_additive_and_the_single_head():
    source = _DISPATCH_MIGRATION.read_text(encoding="utf-8")
    assert _migration_application_dml(source) == []

    config = Config(str(_REPO / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["20260723_approval_dispatch"]
    revision = scripts.get_revision("20260723_approval_dispatch")
    assert revision is not None
    assert revision.down_revision == "20260722_rob1023_widen_runner"


@pytest.mark.unit
@pytest.mark.parametrize(
    "statement",
    [
        "update   review.order_proposals set lifecycle_state = 'x'",
        "InSeRt\nINTO review.order_proposal_approval_batches(id) values (1)",
        "DELETE \n FROM review.order_proposal_approval_dispatch_attempts",
    ],
)
def test_migration_dml_detector_catches_case_and_whitespace_mutations(
    statement: str,
) -> None:
    mutated_source = (
        f"from alembic import op\ndef upgrade():\n    op.execute({statement!r})\n"
    )
    assert _migration_application_dml(mutated_source)


def _valid_dispatch_catalog() -> tuple[
    list[tuple[str, str, str]], list[tuple[str, str, str]]
]:
    columns = [
        (
            "order_proposal_approval_dispatch_attempts",
            column,
            "NO",
        )
        for column in schema_bootstrap._APPROVAL_ATTEMPT_NOT_NULL_COLUMNS
    ]
    constraints = []
    for (
        table_name,
        constraint_name,
    ), (
        column,
        allowed_values,
        nullable_allowed,
    ) in schema_bootstrap._APPROVAL_REQUIRED_CHECKS.items():
        nullable_sql = f"{column} IS NULL OR " if nullable_allowed else ""
        values_sql = ",".join(f"'{value}'" for value in sorted(allowed_values))
        constraints.append(
            (
                table_name,
                constraint_name,
                f"CHECK ({nullable_sql}{column} IN ({values_sql}))",
            )
        )
    return columns, constraints


@pytest.mark.unit
def test_persistent_bootstrap_catalog_matches_typed_dispatch_contract() -> None:
    columns, constraints = _valid_dispatch_catalog()
    assert (
        schema_bootstrap._approval_dispatch_catalog_errors(
            columns=columns,
            constraints=constraints,
        )
        == []
    )
    bootstrap_sql = "\n".join(schema_bootstrap._DDL_STATEMENTS)
    for constraint_name in (
        "order_proposals_approval_dispatch_card_kind",
        "order_proposal_approval_batches_dispatch_state",
        "order_proposal_approval_dispatch_attempt_state",
        "order_proposal_approval_dispatch_attempt_card_kind",
    ):
        assert constraint_name in bootstrap_sql
    for column in schema_bootstrap._APPROVAL_ATTEMPT_NOT_NULL_COLUMNS:
        assert f"ALTER COLUMN {column} SET NOT NULL" in bootstrap_sql


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutation",
    ["invalid_state", "invalid_card_kind", "nullable_binding"],
)
def test_persistent_bootstrap_catalog_rejects_weakened_contract(
    mutation: str,
) -> None:
    columns, constraints = _valid_dispatch_catalog()
    if mutation == "nullable_binding":
        columns = [
            (
                table,
                column,
                ("YES" if column == "membership_digest" else is_nullable),
            )
            for table, column, is_nullable in columns
        ]
    else:
        target = (
            "order_proposal_approval_dispatch_attempt_state"
            if mutation == "invalid_state"
            else "order_proposals_approval_dispatch_card_kind"
        )
        constraints = [
            (
                table,
                name,
                (
                    definition[:-2] + ",'invalid_value'))"
                    if name == target
                    else definition
                ),
            )
            for table, name, definition in constraints
        ]

    assert schema_bootstrap._approval_dispatch_catalog_errors(
        columns=columns,
        constraints=constraints,
    )


@pytest.mark.unit
def test_approval_batch_models_are_durable_and_bound_to_proposals():
    batch = models.OrderProposalApprovalBatch.__table__
    member = models.OrderProposalApprovalBatchMember.__table__

    assert batch.schema == member.schema == "review"
    assert batch.name == "order_proposal_approval_batches"
    assert member.name == "order_proposal_approval_batch_members"
    assert {
        "batch_id",
        "chat_id",
        "window_started_at",
        "window_closes_at",
        "expires_at",
        "approval_nonce",
        "approval_nonce_used_at",
        "approved_by_telegram_user_id",
        "approved_at",
        "summary_message_id",
        "summary_dispatch_state",
        "summary_dispatch_lease_until",
        "approval_dispatch_state",
        "approval_dispatch_attempt_id",
        "approval_dispatch_published_at",
        "membership_revision",
        "membership_digest",
        "membership_frozen_at",
    } <= set(batch.columns.keys())
    assert {
        "batch_pk",
        "proposal_pk",
        "approval_nonce_snapshot",
        "approval_message_id",
        "membership_revision",
        "approval_dispatch_attempt_id_snapshot",
        "approval_membership_revision_snapshot",
        "approval_membership_digest_snapshot",
        "approval_card_kind_snapshot",
        "result",
        "result_detail",
        "processed_at",
        "added_at",
    } <= set(member.columns.keys())

    unique_names = {
        constraint.name
        for constraint in member.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert "uq_order_proposal_batch_member" in unique_names
    assert "uq_order_proposal_batch_member_nonce" in unique_names


@pytest.mark.unit
def test_rung_state_check_covers_all_states():
    # The DB CHECK must list exactly the state-machine's RUNG_STATES.
    # NOTE: Base.metadata's naming_convention (app/models/base.py) rewrites
    # explicit CheckConstraint names to "ck_<table>_<given_name>" at the
    # SQLAlchemy metadata level (this is standard SQLAlchemy behavior for
    # CheckConstraint specifically, and matches the existing repo precedent
    # in tests/test_invest_kr_fundamentals_snapshots_model.py). The Alembic
    # migration below still creates the raw DB constraint literally named
    # "order_proposal_rungs_state" since op.create_table doesn't route
    # through this metadata's naming convention.
    check = next(
        c
        for c in OrderProposalRung.__table__.constraints
        if getattr(c, "name", None)
        == "ck_order_proposal_rungs_order_proposal_rungs_state"
    )
    sqltext = str(check.sqltext)
    for state in RUNG_STATES:
        assert f"'{state}'" in sqltext
