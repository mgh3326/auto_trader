"""CI-only: a bundle-backed executable KR publication round trip (hk 6981)."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.services.daily_plan import store
from app.services.trading_policy_service import load_trading_policy
from tests.services.daily_plan.test_allocation_publication import (
    DAY,
    NOW,
    _bundle,
    _request,
    _row,
    calendar,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _publish(db, request):
    return await store.write_version(
        db,
        day=DAY,
        request=request,
        now=NOW,
        policy=load_trading_policy(),
        policy_hash="hash",
    )


async def _store(db, mode="plan_only"):
    bundle = _bundle()
    bundle["id"] = await store.store_input_bundle(
        db,
        day=DAY,
        body=bundle["body"],
        claimed_sha256=bundle["sha256"],
        mode=mode,
    )
    return bundle


async def _check(db, published, bundle):
    version = (
        (
            await db.execute(
                text(
                    "SELECT v.input_bundle_id, v.input_bundle_sha256, h.mode "
                    "FROM review.daily_plan_versions v JOIN review.daily_plan_heads h "
                    "ON h.id = v.head_id WHERE v.id = :id"
                ),
                {"id": published["id"]},
            )
        )
        .mappings()
        .one()
    )
    assert version["input_bundle_id"] == bundle["id"]
    assert version["input_bundle_sha256"] == bundle["sha256"]
    assert version["mode"] == "executable"
    states = (
        await db.execute(
            text(
                "SELECT DISTINCT execution_state FROM review.daily_plan_rows "
                "WHERE version_id = :id"
            ),
            {"id": published["id"]},
        )
    ).scalars()
    assert list(states) == ["executable"]


async def test_bundle_first_then_executable_publication(db_session, calendar):  # noqa: F811
    async with db_session.begin():
        bundle = await _store(db_session, mode="executable")
        published = await _publish(
            db_session,
            {**_request(bundle, key=str(uuid4())), "mode": "executable"},
        )
        await _check(db_session, published, bundle)
        await db_session.rollback()


async def test_existing_executable_head_then_bundle_backed_version(
    db_session,
    calendar,  # noqa: F811
):
    async with db_session.begin():
        unbound = {
            **_request(_bundle(), key=str(uuid4()), symbols=("005930",)),
            "mode": "executable",
        }
        for key in ("input_bundle_id", "input_bundle_sha256"):
            unbound.pop(key)
        # Without a bundle there is no evidence to route an account.
        unbound["rows"] = [
            {k: v for k, v in _row("005930").items() if k != "would_be_account_ref"}
        ]
        await _publish(db_session, unbound)
        # The bundle job's default mode must still join the executable head.
        bundle = await _store(db_session)
        published = await _publish(
            db_session,
            {
                **_request(bundle, seq=1, key=str(uuid4())),
                "mode": "executable",
                "rows": [_row("005930"), _row("278470")],
            },
        )
        await _check(db_session, published, bundle)
        await db_session.rollback()
