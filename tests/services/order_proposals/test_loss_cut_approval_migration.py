from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.order_proposals import (
    OrderProposal,
    OrderProposalApprovalDispatchAttempt,
    OrderProposalApprovalEvent,
    OrderProposalLossCutScope,
)

_REPO = Path(__file__).resolve().parents[3]
_MIGRATION = _REPO / "alembic/versions/20260814_loss_cut_approval_b1.py"


@pytest.mark.unit
def test_loss_cut_approval_schema_is_typed_and_channel_neutral():
    proposal = OrderProposal.__table__
    attempt = OrderProposalApprovalDispatchAttempt.__table__
    scope = OrderProposalLossCutScope.__table__
    event = OrderProposalApprovalEvent.__table__

    assert {
        "approval_dispatch_channel",
        "approval_dispatch_scope_hash",
        "approval_dispatch_evidence_hash",
        "approved_by_channel",
        "approved_by_subject",
    } <= set(proposal.columns.keys())
    assert {"channel", "scope_hash", "evidence_hash", "publication_ref_digest"} <= set(
        attempt.columns.keys()
    )
    assert attempt.columns["channel"].nullable is False
    assert scope.schema == "review"
    assert scope.columns["proposal_pk"].nullable is False
    assert event.schema == "review"
    assert {
        "event_id",
        "ceremony_digest",
        "actor_subject",
        "scope_hash",
        "evidence_hash",
        "evidence_snapshot",
        "observed_at",
    } <= set(event.columns.keys())


@pytest.mark.unit
def test_loss_cut_approval_migration_is_single_head_and_has_no_row_dml():
    source = _MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignments: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        elif isinstance(node, ast.Assign):
            target = node.targets[0]
            value = node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
            assignments[target.id] = ast.literal_eval(value)
    assert assignments == {
        "revision": "20260814_lcapprove_b1",
        "down_revision": "20260805_toss_merge",
    }
    executed_literals = [
        ast.literal_eval(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert not [
        statement
        for statement in executed_literals
        if re.match(r"^\s*(insert\s+into|update\s+|delete\s+from)", statement, re.I)
    ]
    assert "trg_order_proposal_approval_events_append_only" in source
    assert "trg_order_proposal_approval_events_truncate_append_only" in source

    config = Config(str(_REPO / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["20260815_rob1255_audit"]
