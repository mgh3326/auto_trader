from __future__ import annotations

import datetime as dt
import json
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.jobs import investor_flow_snapshots as job
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.investor_flow_snapshots.builder import InvestorFlowBuildResult
from app.services.investor_flow_snapshots.repository import InvestorFlowSnapshotUpsert
from app.services.naver_finance import investor as naver_investor


class _SessionFactory(AbstractAsyncContextManager):
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def bind_job_session(monkeypatch, db_session):
    monkeypatch.setattr(job, "AsyncSessionLocal", lambda: _SessionFactory(db_session))
    return db_session


def _payload(
    symbol: str, snapshot_date: dt.date = dt.date(2026, 5, 12)
) -> InvestorFlowSnapshotUpsert:
    return InvestorFlowSnapshotUpsert(
        market="kr",
        symbol=symbol,
        snapshot_date=snapshot_date,
        foreign_net=100,
        institution_net=50,
        individual_net=-150,
        source="naver_finance",
        collected_at=dt.datetime(2026, 5, 12, 7, 0, tzinfo=dt.UTC),
    )


@pytest.mark.asyncio
async def test_dry_run_reports_counts_and_idempotency_without_writing(
    bind_job_session, db_session, monkeypatch
):
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_(["900311", "900312"])
        )
    )
    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(
            InvestorFlowSnapshot.symbol.in_(["900311", "900312"])
        )
    )
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="900311", name="ROB205 A", exchange="KOSPI", is_active=True
            ),
            KRSymbolUniverse(
                symbol="900312", name="ROB205 B", exchange="KOSPI", is_active=True
            ),
        ]
    )
    await db_session.commit()

    async def fake_builder(**kwargs):
        return InvestorFlowBuildResult(
            payloads=[_payload(symbol) for symbol in kwargs["symbols"]],
            warnings=("fixture warning",),
        )

    monkeypatch.setattr(job, "build_investor_flow_snapshots", fake_builder)

    result = await job.run_investor_flow_snapshot_build(
        job.InvestorFlowSnapshotBuildRequest(limit=2, commit=False)
    )

    assert result.committed is False
    assert result.symbols_resolved == 2
    assert result.snapshots_built == 2
    assert result.snapshot_date_distribution == {"2026-05-12": 2}
    assert result.idempotency == {
        "wouldInsert": 2,
        "wouldUpdate": 0,
        "duplicatePayloadKeys": 0,
    }
    assert len(result.samples) == 2
    assert result.warnings == ("batch 1: fixture warning",)
    rows = await db_session.execute(
        sa.select(sa.func.count())
        .select_from(InvestorFlowSnapshot)
        .where(InvestorFlowSnapshot.symbol.in_(["900311", "900312"]))
    )
    assert rows.scalar_one() == 0


@pytest.mark.asyncio
async def test_commit_persists_and_second_dry_run_reports_would_update(
    bind_job_session, db_session, monkeypatch
):
    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(InvestorFlowSnapshot.symbol == "900313")
    )
    await db_session.commit()

    async def fake_builder(**kwargs):
        return InvestorFlowBuildResult(payloads=[_payload(kwargs["symbols"][0])])

    monkeypatch.setattr(job, "build_investor_flow_snapshots", fake_builder)
    request = job.InvestorFlowSnapshotBuildRequest(symbols=("900313",), commit=True)

    committed = await job.run_investor_flow_snapshot_build(request)
    assert committed.committed is True
    assert committed.idempotency["wouldInsert"] == 1

    dry_run = await job.run_investor_flow_snapshot_build(
        job.InvestorFlowSnapshotBuildRequest(symbols=("900313",), commit=False)
    )
    assert dry_run.idempotency == {
        "wouldInsert": 0,
        "wouldUpdate": 1,
        "duplicatePayloadKeys": 0,
    }


@pytest.mark.asyncio
async def test_non_kr_market_rejected(bind_job_session):
    with pytest.raises(ValueError, match="Unsupported investor-flow snapshot market"):
        await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(market="us")
        )


# --- #895 zero-commit floor ---------------------------------------------------
# Real XKRX session data (verified against exchange_calendars): Monday
# 2026-09-28 is an open session; Saturday 2026-09-26 and the weekday holidays
# 2026-10-05 (Mon) / 2026-10-09 (Fri) are closed. Using real calendar dates —
# not a monkeypatched classifier — is what pins the gate to XKRX rather than a
# weekday check.
_KR_TRADING_MONDAY = dt.date(2026, 9, 28)
_KR_WEEKEND_SATURDAY = dt.date(2026, 9, 26)
_KR_WEEKDAY_HOLIDAY_FRIDAY = dt.date(2026, 10, 9)
_KR_WEEKDAY_HOLIDAY_MONDAY = dt.date(2026, 10, 5)

_TREND_MALFORMED_FIXTURE = (
    Path(__file__).parent / "fixtures" / "investor_flow" / "005930_trend_malformed.json"
)


async def _empty_builder(**kwargs):
    return InvestorFlowBuildResult(
        payloads=[],
        warnings=tuple(
            f"{symbol}: no investor-flow rows returned" for symbol in kwargs["symbols"]
        ),
    )


@pytest.mark.asyncio
async def test_commit_zero_rows_on_trading_day_fails_loudly(monkeypatch):
    monkeypatch.setattr(job, "build_investor_flow_snapshots", _empty_builder)

    with pytest.raises(
        job.InvestorFlowEmptyCommitError, match=r"KRX trading day 2026-09-28"
    ):
        await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(
                symbols=("005930", "000660"), commit=True, today=_KR_TRADING_MONDAY
            )
        )


@pytest.mark.asyncio
async def test_commit_zero_rows_on_weekend_stays_successful(monkeypatch):
    monkeypatch.setattr(job, "build_investor_flow_snapshots", _empty_builder)

    exc = None
    result = None
    try:
        result = await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(
                symbols=("005930",), commit=True, today=_KR_WEEKEND_SATURDAY
            )
        )
    except Exception as e:  # noqa: BLE001
        exc = e

    assert exc is None
    assert result is not None
    assert result.snapshots_built == 0
    assert result.committed is True
    assert result.warnings == ("batch 1: 005930: no investor-flow rows returned",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "closed_weekday", [_KR_WEEKDAY_HOLIDAY_FRIDAY, _KR_WEEKDAY_HOLIDAY_MONDAY]
)
async def test_commit_zero_rows_on_weekday_holiday_stays_successful(
    monkeypatch, closed_weekday
):
    # A weekday check would raise here; XKRX says these are closed sessions.
    monkeypatch.setattr(job, "build_investor_flow_snapshots", _empty_builder)

    exc = None
    result = None
    try:
        result = await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(
                symbols=("005930",), commit=True, today=closed_weekday
            )
        )
    except Exception as e:  # noqa: BLE001
        exc = e

    assert exc is None
    assert result is not None
    assert result.snapshots_built == 0


@pytest.mark.asyncio
async def test_commit_partial_success_is_not_counted_as_zero(
    bind_job_session, db_session, monkeypatch
):
    symbols = ("900314", "900315")
    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(InvestorFlowSnapshot.symbol.in_(symbols))
    )
    await db_session.commit()

    async def partial_builder(**kwargs):
        batch = kwargs["symbols"]
        return InvestorFlowBuildResult(
            payloads=[_payload(batch[0])],
            warnings=tuple(
                f"{symbol}: no investor-flow rows returned" for symbol in batch[1:]
            ),
        )

    monkeypatch.setattr(job, "build_investor_flow_snapshots", partial_builder)

    exc = None
    result = None
    try:
        result = await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(
                symbols=symbols, commit=True, today=_KR_TRADING_MONDAY
            )
        )
    except Exception as e:  # noqa: BLE001
        exc = e

    assert exc is None
    assert result is not None
    assert result.snapshots_built == 1
    assert result.committed is True


@pytest.mark.asyncio
async def test_dry_run_zero_rows_on_trading_day_stays_successful(monkeypatch):
    # commit=False is the approval-packet path; the floor must stay silent there.
    monkeypatch.setattr(job, "build_investor_flow_snapshots", _empty_builder)

    exc = None
    result = None
    try:
        result = await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(
                symbols=("005930",), commit=False, today=_KR_TRADING_MONDAY
            )
        )
    except Exception as e:  # noqa: BLE001
        exc = e

    assert exc is None
    assert result is not None
    assert result.committed is False
    assert result.snapshots_built == 0


@pytest.mark.asyncio
async def test_commit_zero_rows_unclassifiable_date_fails_closed(monkeypatch):
    # Fail-closed: an XKRX calendar that cannot classify the run date cannot
    # excuse an empty commit (session_calendar contract).
    monkeypatch.setattr(job, "build_investor_flow_snapshots", _empty_builder)
    monkeypatch.setattr(job, "trading_session_status", lambda *a, **kw: "unknown")

    with pytest.raises(job.InvestorFlowEmptyCommitError, match="could not classify"):
        await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(
                symbols=("005930",), commit=True, today=_KR_TRADING_MONDAY
            )
        )


@pytest.mark.asyncio
async def test_commit_empty_universe_on_trading_day_fails_loudly(monkeypatch):
    async def no_symbols(market):
        return []

    monkeypatch.setattr(job, "resolve_active_universe", no_symbols)
    monkeypatch.setattr(job, "build_investor_flow_snapshots", _empty_builder)

    with pytest.raises(
        job.InvestorFlowEmptyCommitError, match=r"KRX trading day 2026-09-28"
    ):
        await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(
                all_symbols=True, commit=True, today=_KR_TRADING_MONDAY
            )
        )


@pytest.mark.asyncio
async def test_commit_empty_universe_on_closed_day_stays_successful(monkeypatch):
    async def no_symbols(market):
        return []

    monkeypatch.setattr(job, "resolve_active_universe", no_symbols)

    result = await job.run_investor_flow_snapshot_build(
        job.InvestorFlowSnapshotBuildRequest(
            all_symbols=True, commit=True, today=_KR_WEEKEND_SATURDAY
        )
    )

    assert result.snapshots_built == 0
    assert result.warnings == ("no symbols resolved",)


@pytest.mark.asyncio
async def test_empty_upstream_payload_end_to_end_fails_loudly(monkeypatch):
    # End-to-end #895/#900 chain: real trend-JSON parser over an empty payload,
    # real builder, real job — the same chain that must fail loudly when every
    # symbol yields no rows on a trading day.
    async def mock_fetch_json(url, params=None):
        return []

    monkeypatch.setattr(naver_investor, "_fetch_json", mock_fetch_json)

    with pytest.raises(
        job.InvestorFlowEmptyCommitError, match=r"KRX trading day 2026-09-28"
    ):
        await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(
                symbols=("005930",), commit=True, today=_KR_TRADING_MONDAY
            )
        )


@pytest.mark.asyncio
async def test_all_invalid_rows_end_to_end_fails_loudly(monkeypatch):
    # #900: every trend row malformed -> all skipped with counted reasons ->
    # zero payloads -> the #895 gate must still fire (not silently succeed).
    payload = json.loads(_TREND_MALFORMED_FIXTURE.read_text(encoding="utf-8"))
    # Drop the fixture's single valid row so zero payloads are built.
    all_invalid = payload[:-1]

    async def mock_fetch_json(url, params=None):
        return all_invalid

    monkeypatch.setattr(naver_investor, "_fetch_json", mock_fetch_json)

    with pytest.raises(
        job.InvestorFlowEmptyCommitError, match=r"KRX trading day 2026-09-28"
    ):
        await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(
                symbols=("005930",), commit=True, today=_KR_TRADING_MONDAY
            )
        )
