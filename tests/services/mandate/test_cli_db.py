"""hk 3795 T2: the operator CLI on the run-owned test database (CI only).

Runs in CI against the run-owned test database. Locally this module is collected
but not run (hk host safety: no local database). Each test checks what the CLI
wrote, not what it printed: a preview must leave no row, a commit must leave the
row the store writes.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal, engine
from app.models.mandate import MandateApprovalRequest, MandatePlanEvent
from app.services.mandate import store as ms
from scripts import mandate as cli
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.mandate.db_support import (
    EFFECTIVE,
    RULE,
    TRANCHE_RULE,
    MandateSeed,
    new_symbol,
)

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
ACTOR = "operator:cli:db-test"


@pytest_asyncio.fixture
async def seed(db_session):
    helper = MandateSeed(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


def _clause(tmp_path, symbol: str) -> str:
    path = tmp_path / f"clause-{uuid.uuid4().hex[:8]}.json"
    path.write_text(
        json.dumps(
            {
                "basket_key": f"b-{uuid.uuid4().hex[:10]}",
                "account_ref": f"acct-{uuid.uuid4().hex[:6]}",
                "universe_rule": "test universe",
                "allowed_symbols": [symbol],
                "entry_max_premium_bps": "30",
                "per_order_cap": "2000000",
                "daily_cap": "5000000",
                "sector_cap_pct": "35",
                "basket_drawdown_freeze_pct": "25",
                "account_drawdown_freeze_pct": "25",
                "tranche_rule": TRANCHE_RULE,
                "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
                "budget_cap": "10000000",
                "loss_limit": "500000",
                "prereg": {
                    "metric": "cost-net result at H minus baseline",
                    "min_effect_pct": "1",
                    "horizon_sessions": 20,
                    "max_duration_days": 90,
                },
            }
        )
    )
    return str(path)


async def _requests(db, sleeve_id: int) -> int:
    return int(
        await db.scalar(
            sa.select(sa.func.count())
            .select_from(MandateApprovalRequest)
            .where(MandateApprovalRequest.sleeve_id == sleeve_id)
        )
    )


async def _events(db, plan_id: int) -> list[str]:
    rows = await db.execute(
        sa.select(MandatePlanEvent.event)
        .where(MandatePlanEvent.plan_id == plan_id)
        .order_by(MandatePlanEvent.seq)
    )
    return list(rows.scalars().all())


def _args(*argv: str):
    return cli._parser().parse_args(["--database-url-env", "MANDATE_TEST_DB", *argv])


async def test_a_preview_request_terms_writes_no_approval_request(
    seed, db_session, tmp_path
) -> None:
    sleeve = await seed.sleeve(
        state="shadow", budget_cap="10000000", loss_limit="500000", rule_id=RULE
    )
    before = await _requests(db_session, sleeve.id)
    code, payload = await cli.run(
        _args(
            "request-terms",
            "--sleeve-key",
            sleeve.sleeve_key,
            "--input",
            _clause(tmp_path, new_symbol()),
            "--approval-ref",
            "hk:5638",
            "--actor",
            ACTOR,
        ),
        session_factory=AsyncSessionLocal,
    )
    assert code == cli.EXIT_OK
    assert payload["mode"] == "preview"
    assert await _requests(db_session, sleeve.id) == before == 0


async def test_a_commit_request_terms_writes_one_request(
    seed, db_session, tmp_path
) -> None:
    sleeve = await seed.sleeve(
        state="shadow", budget_cap="10000000", loss_limit="500000", rule_id=RULE
    )
    _, payload = await cli.run(
        _args(
            "request-terms",
            "--sleeve-key",
            sleeve.sleeve_key,
            "--input",
            _clause(tmp_path, new_symbol()),
            "--approval-ref",
            "hk:5638",
            "--actor",
            ACTOR,
            "--commit",
        ),
        session_factory=AsyncSessionLocal,
    )
    assert payload["mode"] == "commit"
    assert await _requests(db_session, sleeve.id) == 1
    row = await db_session.get(MandateApprovalRequest, payload["request_id"])
    assert row is not None and row.requested_by == ACTOR


async def test_a_system_actor_writes_nothing_even_with_commit(
    seed, db_session, tmp_path
) -> None:
    sleeve = await seed.sleeve(
        state="shadow", budget_cap="10000000", loss_limit="500000", rule_id=RULE
    )
    with pytest.raises(cli.MandateCliError):
        await cli.run(
            _args(
                "request-terms",
                "--sleeve-key",
                sleeve.sleeve_key,
                "--input",
                _clause(tmp_path, new_symbol()),
                "--approval-ref",
                "hk:5638",
                "--actor",
                "system:mandate_projection",
                "--commit",
            ),
            session_factory=AsyncSessionLocal,
        )
    assert await _requests(db_session, sleeve.id) == 0


async def test_a_preview_event_writes_nothing_and_a_commit_appends_one(
    seed, db_session
) -> None:
    symbol = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(symbol,))
    plan = await seed.register(sleeve, terms, symbol)
    # operator-side approval of the plan; the T3a card does this in production
    await ms.append_plan_event(
        db_session,
        plan_id=plan.plan.id,
        event="approved",
        actor="operator:test",
        terms_id=terms.id,
        effective_session=EFFECTIVE,
    )
    await db_session.commit()
    argv = [
        "event",
        "--plan-id",
        str(plan.plan.id),
        "--event",
        "suspended",
        "--reason",
        "db test hold",
        "--actor",
        ACTOR,
    ]
    before = await _events(db_session, plan.plan.id)

    await cli.run(_args(*argv), session_factory=AsyncSessionLocal)
    assert await _events(db_session, plan.plan.id) == before

    _, payload = await cli.run(
        _args(*argv, "--commit"), session_factory=AsyncSessionLocal
    )
    after = await _events(db_session, plan.plan.id)
    assert after == [*before, "suspended"]
    assert payload["event"] == "suspended" and payload["seq"] == len(after)
