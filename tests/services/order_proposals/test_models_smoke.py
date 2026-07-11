import pytest

from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.order_proposals.state_machine import RUNG_STATES


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
