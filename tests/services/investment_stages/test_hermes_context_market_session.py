"""ROB-366 B6 / ROB-374 B6 — Hermes context market_session.

An operator/Hermes-recorded session on the latest InvestmentStageRun wins.
When no run records one, the exporter now derives the session from the bundle's
own ``as_of`` (ROB-374) instead of leaving it ``None`` — so an
``intraday_action_report_v1`` carries a session whenever the as-of instant is a
determinable trading-session moment. Closed/unknown instants stay ``None``.
DB-backed because the exporter skips run-loading under a mock session.
"""

import datetime as dt
import uuid

import pytest

from app.models.investment_snapshots import InvestmentSnapshotBundle
from app.models.investment_stages import InvestmentStageRun
from app.services.investment_stages.hermes_context import HermesContextExporter

# 2026-05-29 19:39 UTC == ET Fri 15:39 -> regular session (the live ROB-374 bundle).
_REGULAR_AS_OF = dt.datetime(2026, 5, 29, 19, 39, tzinfo=dt.UTC)
# 2026-05-30 (Sat) -> not a trading session -> derivation yields None.
_CLOSED_AS_OF = dt.datetime(2026, 5, 30, 17, 0, tzinfo=dt.UTC)


def _bundle(*, as_of: dt.datetime = _CLOSED_AS_OF) -> InvestmentSnapshotBundle:
    return InvestmentSnapshotBundle(
        bundle_uuid=uuid.uuid4(),
        purpose="report_generation",
        market="us",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
        as_of=as_of,
        status="complete",
        coverage_summary={},
        freshness_summary={},
        idempotency_key=str(uuid.uuid4()),
    )


def _run(
    bundle_uuid: uuid.UUID,
    *,
    market_session: str | None,
    started_at: dt.datetime | None = None,
) -> InvestmentStageRun:
    return InvestmentStageRun(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=bundle_uuid,
        market="us",
        market_session=market_session,
        account_scope="kis_live",
        policy_version="v1",
        generator_version="v1",
        status="completed",
        started_at=started_at or dt.datetime.now(tz=dt.UTC),
    )


@pytest.mark.asyncio
async def test_recorded_run_session_wins_over_derivation(db_session) -> None:
    # Run records "regular" while the bundle as_of is a closed (weekend) instant:
    # the explicit run value must win, proving recorded > derived precedence.
    bundle = _bundle(as_of=_CLOSED_AS_OF)
    db_session.add(bundle)
    await db_session.commit()
    db_session.add(_run(bundle.bundle_uuid, market_session="regular"))
    await db_session.commit()

    payload = await HermesContextExporter(db_session, stages=[]).export(
        snapshot_bundle_uuid=bundle.bundle_uuid
    )
    assert payload.market_session == "regular"


@pytest.mark.asyncio
async def test_session_derived_from_bundle_as_of_when_no_run(db_session) -> None:
    bundle = _bundle(as_of=_REGULAR_AS_OF)
    db_session.add(bundle)
    await db_session.commit()

    payload = await HermesContextExporter(db_session, stages=[]).export(
        snapshot_bundle_uuid=bundle.bundle_uuid
    )
    assert payload.market_session == "regular"


@pytest.mark.asyncio
async def test_session_none_when_no_run_and_as_of_closed(db_session) -> None:
    bundle = _bundle(as_of=_CLOSED_AS_OF)
    db_session.add(bundle)
    await db_session.commit()

    payload = await HermesContextExporter(db_session, stages=[]).export(
        snapshot_bundle_uuid=bundle.bundle_uuid
    )
    assert payload.market_session is None


@pytest.mark.asyncio
async def test_session_derived_when_run_recorded_none(db_session) -> None:
    # A run that recorded None means "not captured", not "no session": the
    # exporter falls through to deriving from the bundle as_of.
    bundle = _bundle(as_of=_REGULAR_AS_OF)
    db_session.add(bundle)
    await db_session.commit()
    db_session.add(_run(bundle.bundle_uuid, market_session=None))
    await db_session.commit()

    payload = await HermesContextExporter(db_session, stages=[]).export(
        snapshot_bundle_uuid=bundle.bundle_uuid
    )
    assert payload.market_session == "regular"


@pytest.mark.asyncio
async def test_session_uses_latest_run_when_multiple(db_session) -> None:
    # Bundle re-run across sessions: the most recent run (by started_at) wins.
    bundle = _bundle(as_of=_CLOSED_AS_OF)
    db_session.add(bundle)
    await db_session.commit()
    now = dt.datetime.now(tz=dt.UTC)
    db_session.add(
        _run(
            bundle.bundle_uuid,
            market_session="pre",
            started_at=now - dt.timedelta(hours=2),
        )
    )
    db_session.add(_run(bundle.bundle_uuid, market_session="regular", started_at=now))
    await db_session.commit()

    payload = await HermesContextExporter(db_session, stages=[]).export(
        snapshot_bundle_uuid=bundle.bundle_uuid
    )
    assert payload.market_session == "regular"
