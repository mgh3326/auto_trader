"""ROB-1470 sleeve store, DB guards and fill attribution on the test database.

Runs in CI against the run-owned test database (never a production database).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from app.core.db import engine
from app.models.order_proposals import OrderProposal
from app.services.order_proposals.errors import OrderProposalError
from app.services.strategy_sleeves import store
from app.services.strategy_sleeves.book import compute_sleeve_book
from app.services.strategy_sleeves.vocabulary import SYSTEM_LOSS_GATE_ACTOR
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.strategy_sleeves.db_support import (
    SleeveSeed,
    raw,
    unique_order_id,
    unique_symbol,
)

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def seed(db_session):
    helper = SleeveSeed(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


async def test_versions_append_and_copy_unchanged_fields(db_session, seed) -> None:
    sleeve = await seed.sleeve(state="shadow", budget_cap="1000", loss_limit="100")
    await store.set_sleeve_version(
        db_session, sleeve_id=sleeve.id, budget_cap="2000", reason="r", actor="op"
    )
    await store.set_sleeve_version(
        db_session, sleeve_id=sleeve.id, state="live", reason="flip", actor="op"
    )
    await db_session.commit()

    rows = await store.history(db_session, sleeve.id)
    assert [
        (r.version, r.state, r.budget_cap, r.loss_limit, r.set_by) for r in rows
    ] == [
        (1, "shadow", Decimal("1000"), Decimal("100"), "test"),
        (2, "shadow", Decimal("2000"), Decimal("100"), "op"),
        (3, "live", Decimal("2000"), Decimal("100"), "op"),
    ]
    current = await store.current_version(db_session, sleeve.id)
    assert current is not None and current.version == 3

    with pytest.raises(store.SleeveError, match="nothing changes"):
        await store.set_sleeve_version(
            db_session, sleeve_id=sleeve.id, state="live", reason="r", actor="op"
        )
    with pytest.raises(store.SleeveError, match="reserved"):
        await store.set_sleeve_version(
            db_session,
            sleeve_id=sleeve.id,
            state="stopped",
            reason="r",
            actor=SYSTEM_LOSS_GATE_ACTOR,
        )
    await db_session.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE review.strategy_sleeve_versions SET budget_cap = 1 WHERE sleeve_id = :id",
        "DELETE FROM review.strategy_sleeve_versions WHERE sleeve_id = :id",
        "UPDATE review.strategy_sleeves SET rule_id = 'x' WHERE id = :id",
        "DELETE FROM review.strategy_sleeves WHERE id = :id",
        "INSERT INTO review.strategy_sleeve_versions "
        "(sleeve_id, version, state, budget_cap, loss_limit, reason, set_by) "
        "VALUES (:id, 5, 'live', 1, 1, 'skip', 'op')",
        "INSERT INTO review.strategy_sleeve_versions "
        "(sleeve_id, version, state, budget_cap, loss_limit, reason, set_by) "
        "VALUES (:id, 1, 'live', 1, 1, 'rewrite', 'op')",
    ],
)
async def test_the_db_refuses_to_rewrite_sleeve_history(
    db_session, seed, statement
) -> None:
    # A rollback expires ORM objects; keep plain ids, never re-read attributes.
    sleeve_id = (await seed.sleeve()).id
    with pytest.raises(DBAPIError):
        await raw(db_session, statement, id=sleeve_id)
    await db_session.rollback()
    assert [r.version for r in await store.history(db_session, sleeve_id)] == [1]


async def test_create_refuses_amounts_and_rules_it_cannot_use(db_session, seed) -> None:
    bad = [
        {"budget_cap": "-1"},
        {"loss_limit": "0"},
        {"budget_cap": "NaN"},
        {"budget_cap": None},
        {"rule_id": "made.up"},
        {"market": "equity_us"},  # kr_cut_minus20 is KR only
        {"state": "stopped"},
        {"account_mode": "upbit"},  # upbit/equity_kr is not an account market
    ]
    for override in bad:
        values = {
            "sleeve_key": "never-written",
            "rule_id": "rob1458.kr_cut_minus20",
            "account_mode": "kis_live",
            "market": "equity_kr",
            "state": "shadow",
            "budget_cap": "1",
            "loss_limit": "1",
            "reason": "r",
            "actor": "op",
            **override,
        }
        with pytest.raises(store.SleeveError):
            await store.create_sleeve(db_session, **values)
    await db_session.rollback()
    assert await store.get_sleeve_by_key(db_session, "never-written") is None


async def test_a_proposal_tag_binds_account_and_market_and_never_changes(
    db_session, seed
) -> None:
    # A rollback expires ORM objects; keep plain ids, never re-read attributes.
    kr_id = (await seed.sleeve()).id
    other_id = (await seed.sleeve()).id
    us_id = (
        await seed.sleeve(market="equity_us", rule_id="rob1458.us_near_market_entry")
    ).id
    symbol = unique_symbol()

    with pytest.raises(OrderProposalError, match="not kis_live/equity_kr"):
        await seed.proposal(sleeve_id=us_id, symbol=symbol, commit=False)
    await db_session.rollback()

    group = await seed.proposal(sleeve_id=kr_id, symbol=symbol)
    group_pk, group_sleeve_id = group.id, group.sleeve_id
    assert group_sleeve_id == kr_id
    for value in (other_id, None):
        with pytest.raises(DBAPIError, match="cannot change once set"):
            await db_session.execute(
                sa.update(OrderProposal)
                .where(OrderProposal.id == group_pk)
                .values(sleeve_id=value)
            )
        await db_session.rollback()

    # The trigger itself also refuses a cross-market tag that skips the service.
    with pytest.raises(DBAPIError, match="another account or market"):
        await raw(
            db_session,
            "INSERT INTO review.order_proposals (proposal_id, root_proposal_id, "
            "symbol, market, account_mode, side, order_type, proposer, sleeve_id) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), :s, 'equity_kr', "
            "'kis_live', 'buy', 'limit', 'p', :sleeve)",
            s=symbol,
            sleeve=us_id,
        )
    await db_session.rollback()

    # An untagged proposal's other columns still update freely.
    untagged = await seed.proposal(sleeve_id=None, symbol=symbol)
    await db_session.execute(
        sa.update(OrderProposal)
        .where(OrderProposal.id == untagged.id)
        .values(thesis="edited")
    )
    await db_session.commit()


async def test_a_revision_stays_in_its_sleeve(db_session, seed) -> None:
    sleeve = await seed.sleeve()
    other = await seed.sleeve()
    symbol = unique_symbol()
    first = await seed.proposal(sleeve_id=sleeve.id, symbol=symbol)

    revised = await seed.proposal(
        sleeve_id=None, symbol=symbol, supersedes=first.proposal_id
    )
    assert revised.sleeve_id == sleeve.id

    with pytest.raises(OrderProposalError, match="keep the superseded"):
        await seed.proposal(
            sleeve_id=other.id,
            symbol=symbol,
            supersedes=revised.proposal_id,
            commit=False,
        )
    await db_session.rollback()


async def test_a_fill_on_a_tagged_order_updates_only_that_sleeve(
    db_session, seed
) -> None:
    """Acceptance 3."""
    sleeve_a = await seed.sleeve()
    sleeve_b = await seed.sleeve()
    symbol = unique_symbol()
    order_a, order_b, order_untagged = (unique_order_id() for _ in range(3))
    group_a = await seed.proposal(sleeve_id=sleeve_a.id, symbol=symbol)
    group_b = await seed.proposal(sleeve_id=sleeve_b.id, symbol=symbol)
    untagged = await seed.proposal(sleeve_id=None, symbol=symbol)
    await seed.mark_acked(group_a, order_a)
    await seed.mark_acked(group_b, order_b)
    await seed.mark_acked(untagged, order_untagged)

    # The helper rolls back after each read, which expires ORM objects: load
    # the sleeve fresh by id every time.
    sleeve_a_id, sleeve_b_id = sleeve_a.id, sleeve_b.id

    async def book(sleeve_id):
        sleeve = await store.get_sleeve(db_session, sleeve_id)
        assert sleeve is not None
        inputs = await store.load_sleeve_inputs(db_session, sleeve)
        await db_session.rollback()
        return compute_sleeve_book(
            orders=inputs.orders, fills=inputs.fills, marks={symbol: Decimal("90")}
        )

    before_b = await book(sleeve_b_id)
    await seed.fill(order_id=order_a, symbol=symbol, quantity="10", price="100")
    await seed.fill(order_id=order_untagged, symbol=symbol, quantity="7", price="100")
    # A provisional websocket row on B's order is not a fill until reconciled.
    await seed.fill(
        order_id=order_b, symbol=symbol, quantity="3", price="100", source="websocket"
    )

    after_a = await book(sleeve_a_id)
    after_b = await book(sleeve_b_id)
    [position] = after_a.positions
    assert (position.quantity, position.cost_basis) == (Decimal("10"), Decimal("1000"))
    assert after_a.net_pnl == Decimal("-100")
    assert after_a.trades == 1
    assert after_b.positions == ()
    assert after_b.trades == before_b.trades == 0
    assert after_b.net_pnl == Decimal("0")
    # B's acked order is still an open buy order: its budget use is unchanged.
    assert (
        after_b.open_order_notional == before_b.open_order_notional == Decimal("1000")
    )


async def test_live_needs_a_complete_preregistration(db_session, seed) -> None:
    """ROB-1468: metric, minimum effect, H and max duration before any live order."""
    with pytest.raises(store.SleeveError, match="pre-registration"):
        await store.create_sleeve(
            db_session,
            sleeve_key="never-live",
            rule_id="rob1458.kr_cut_minus20",
            account_mode="kis_live",
            market="equity_kr",
            state="live",
            budget_cap="1",
            loss_limit="1",
            reason="r",
            actor="op",
            metric="m",
        )
    await db_session.rollback()
    sleeve_id = (await seed.sleeve(state="shadow")).id
    # The CHECK holds for a writer that skips the store too.
    with pytest.raises(DBAPIError, match="live_requires_prereg"):
        await raw(
            db_session,
            "INSERT INTO review.strategy_sleeve_versions "
            "(sleeve_id, version, state, budget_cap, loss_limit, reason, set_by) "
            "VALUES (:id, 2, 'live', 1, 1, 'no prereg', 'op')",
            id=sleeve_id,
        )
    await db_session.rollback()


async def test_preregistration_is_frozen_once_the_sleeve_has_been_live(
    db_session, seed
) -> None:
    # A rollback expires ORM objects; keep plain ids, never re-read attributes.
    sleeve_id = (await seed.sleeve(state="shadow")).id
    # Before the first live version the operator may still revise it.
    await store.set_sleeve_version(
        db_session, sleeve_id=sleeve_id, horizon_sessions="10", reason="r", actor="op"
    )
    await store.set_sleeve_version(
        db_session, sleeve_id=sleeve_id, state="live", reason="go", actor="op"
    )
    await store.set_sleeve_version(
        db_session, sleeve_id=sleeve_id, state="paused", reason="hold", actor="op"
    )
    await db_session.commit()

    with pytest.raises(store.SleeveError, match="frozen"):
        await store.set_sleeve_version(
            db_session, sleeve_id=sleeve_id, metric="other", reason="r", actor="op"
        )
    await db_session.rollback()
    with pytest.raises(DBAPIError, match="frozen once the sleeve has been live"):
        await raw(
            db_session,
            "INSERT INTO review.strategy_sleeve_versions "
            "(sleeve_id, version, state, budget_cap, loss_limit, prereg_metric, "
            "prereg_min_effect_pct, prereg_horizon_sessions, "
            "prereg_max_duration_days, reason, set_by) "
            "VALUES (:id, 5, 'paused', 1000, 100, 'other', 1, 10, 90, 'r', 'op')",
            id=sleeve_id,
        )
    await db_session.rollback()
    # Amounts and state still change; the pre-registration is copied.
    row = await store.set_sleeve_version(
        db_session, sleeve_id=sleeve_id, budget_cap="2000", reason="r", actor="op"
    )
    assert (row.version, row.prereg_horizon_sessions) == (5, 10)
    await db_session.commit()
