"""ROB-3873 data_gaps sources: crypto indicators, candles, market events, research
reports, valuation snapshots. Fake sessions only; no DB, network or broker."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as ns

import pytest

from app.mcp_server.tooling.fundamentals import _crypto_regime
from app.services.retro_v1 import read_data_gaps
from app.services.retro_v1.data_gaps import (
    CRYPTO_OI_DISABLED,
    CRYPTO_REGIME_STALE_AFTER_SECONDS,
    crypto_insight_state,
)
from app.services.retro_v1.envelope import KST
from tests.services.retro_v1.support import WithParams, operational_error, validate
from tests.services.retro_v1.test_other_readers import (
    _flat,
    crypto_snapshot,
    gap_session,
)

pytestmark = pytest.mark.unit

NOW = datetime.now(UTC)
REGIME_SOURCE = Path(_crypto_regime.__file__).read_text(encoding="utf-8")


def crypto_route(rows):
    def route(sql: str, params: dict):
        wanted = _flat(params)
        return [r for r in rows if r.metric in wanted and r.provider in wanted]

    return WithParams(route)


def by_dataset(response):
    return {row["dataset"]: row for row in response["data"]}


# --------------------------------------------------------------------------
# crypto indicators
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fear_greed_70_days_old_is_a_stale_row_and_fresh_tvl_is_not() -> None:
    old_fear = crypto_snapshot(
        "fear_greed",
        "alternative_me",
        snapshot_at=NOW - timedelta(days=70),
    )
    fresh_tvl = crypto_snapshot(
        "tvl", "defillama", symbol="bitcoin", snapshot_at=NOW - timedelta(hours=1)
    )
    session = gap_session(
        crypto_insight_snapshots=crypto_route([old_fear, fresh_tvl]),
    )
    response = await read_data_gaps(session)
    validate(response, "DataGapsResponse")
    rows = by_dataset(response)

    fear = rows["crypto_insight.fear_greed"]
    assert fear["missing_reason"] == "stale"
    assert fear["last_updated_at"] is not None
    assert fear["source"] == "alternative_me"
    assert "crypto_insight.defi_tvl" not in rows


@pytest.mark.asyncio
async def test_disabled_open_interest_is_an_unsupported_row() -> None:
    response = await read_data_gaps(gap_session())
    validate(response, "DataGapsResponse")
    oi = by_dataset(response)["crypto_insight.aggregate_oi"]
    assert oi["missing_reason"] == "unsupported"
    assert oi["last_updated_at"] is None
    assert oi["source"] == "coinglass"
    assert oi["basis"] == "inference"


@pytest.mark.asyncio
async def test_a_snapshot_inside_its_own_freshness_window_is_not_a_gap() -> None:
    # 2 h old: stale against its own 1 h window, fresh against the 24 h default.
    tight = crypto_snapshot(
        "fear_greed",
        "alternative_me",
        snapshot_at=NOW - timedelta(hours=2),
        freshness_seconds=3600,
    )
    loose = crypto_snapshot(
        "stablecoin_supply",
        "defillama",
        snapshot_at=NOW - timedelta(hours=2),
    )
    session = gap_session(
        crypto_insight_snapshots=crypto_route([tight, loose]),
    )
    rows = by_dataset(await read_data_gaps(session))
    assert rows["crypto_insight.fear_greed"]["missing_reason"] == "stale"
    assert "crypto_insight.stablecoin_supply" not in rows


@pytest.mark.asyncio
async def test_a_missing_crypto_indicator_is_missing_data_not_fresh() -> None:
    session = gap_session(crypto_insight_snapshots=crypto_route([]))
    response = await read_data_gaps(session)
    validate(response, "DataGapsResponse")
    row = by_dataset(response)["crypto_insight.breadth"]
    assert row["missing_reason"] == "missing_data"
    assert row["last_updated_at"] is None


# --------------------------------------------------------------------------
# candles: stale against the last confirmed session / crypto's last closed day
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_minute_and_daily_bars_older_than_the_last_session_are_stale() -> None:
    ten_days_ago = NOW - timedelta(days=10)
    session = gap_session(
        **{
            "FROM public.kr_candles_1m": [ns(latest=ten_days_ago)],
            "FROM public.crypto_candles_1d": [ns(latest=ten_days_ago)],
        }
    )
    response = await read_data_gaps(session)
    validate(response, "DataGapsResponse")
    rows = by_dataset(response)
    assert rows["candles_1m.kr"]["missing_reason"] == "stale"
    assert rows["candles_1m.kr"]["last_updated_at"] is not None
    assert rows["candles_1d.crypto"]["missing_reason"] == "stale"
    assert "candles_1m.us" not in rows  # still fresh in this fixture


@pytest.mark.asyncio
async def test_bars_with_no_rows_at_all_are_missing_data() -> None:
    session = gap_session(**{"FROM public.us_candles_1m": [ns(latest=None)]})
    rows = by_dataset(await read_data_gaps(session))
    assert rows["candles_1m.us"]["missing_reason"] == "missing_data"


# --------------------------------------------------------------------------
# market events: a failure text never reaches the response
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failed_market_event_partition_is_a_row_and_its_error_never_leaks() -> (
    None
):
    secret = "SECRET-PROVIDER-ERROR-TEXT-4242 https://provider.example/key=abc"
    now = NOW
    today = now.astimezone(KST).date()
    yesterday = today - timedelta(days=1)
    dart = [
        ns(
            source="dart",
            category="disclosure",
            market="kr",
            partition_date=yesterday,
            status="succeeded",
            finished_at=now,
            last_error=None,
            event_count=3,
        ),
        ns(
            source="dart",
            category="disclosure",
            market="kr",
            partition_date=today,
            status="failed",
            finished_at=now,
            last_error=secret,
            event_count=0,
        ),
    ]
    others = [
        ns(
            source=s,
            category=c,
            market=m,
            partition_date=today,
            status="succeeded",
            finished_at=now,
            last_error=None,
            event_count=1,
        )
        for s, c, m in (
            ("finnhub", "earnings", "us"),
            ("forexfactory", "economic", "global"),
            ("wisefn", "earnings", "kr"),
        )
    ]
    session = gap_session(**{"FROM market_event_ingestion_partitions": dart + others})
    response = await read_data_gaps(session)
    validate(response, "DataGapsResponse")
    row = by_dataset(response)["market_events.dart.disclosure.kr"]
    assert row["missing_reason"] == "incomplete_evidence"
    assert secret not in json.dumps(response)
    assert "provider.example" not in json.dumps(response)


@pytest.mark.asyncio
async def test_an_expected_market_event_source_with_no_partitions_is_missing() -> None:
    session = gap_session(**{"FROM market_event_ingestion_partitions": []})
    response = await read_data_gaps(session)
    rows = by_dataset(response)
    assert rows["market_events.finnhub.earnings.us"]["missing_reason"] == "missing_data"


# --------------------------------------------------------------------------
# one source failing is its own unknown; the others still report
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_one_db_failure_is_an_unknown_and_other_rows_remain() -> None:
    session = gap_session(**{"FROM public.kr_candles_1m": operational_error()})
    response = await read_data_gaps(session)
    validate(response, "DataGapsResponse")
    assert response["total"] is None
    fields = {(u["field"], u["reason"]) for u in response["unknowns"]}
    assert ("source.candles_1m.kr", "upstream_error") in fields
    assert ("total", "count_unavailable") in fields
    datasets = {row["dataset"] for row in response["data"]}
    assert "candles_1m.kr" not in datasets
    assert "crypto_insight.aggregate_oi" in datasets
    assert "negative_class_recording.kr" in datasets


@pytest.mark.asyncio
async def test_a_non_db_exception_from_one_source_is_contained_and_not_echoed() -> None:
    boom = RuntimeError("secret-boom-detail https://internal.example/x")
    session = gap_session(**{"FROM research_report_ingestion_runs": boom})
    response = await read_data_gaps(session)
    validate(response, "DataGapsResponse")
    assert response["total"] is None
    assert ("source.research_reports", "upstream_error") in {
        (u["field"], u["reason"]) for u in response["unknowns"]
    }
    serialized = json.dumps(response)
    assert "secret-boom-detail" not in serialized
    assert "internal.example" not in serialized
    assert "research_reports" not in {row["dataset"] for row in response["data"]}
    assert "crypto_insight.aggregate_oi" in {row["dataset"] for row in response["data"]}


# --------------------------------------------------------------------------
# pin tests: the copied rules must match the regime view they came from
# --------------------------------------------------------------------------
def test_the_copied_crypto_threshold_is_the_regime_views_threshold() -> None:
    assert (
        CRYPTO_REGIME_STALE_AFTER_SECONDS == _crypto_regime._REGIME_STALE_AFTER_SECONDS
    )


@pytest.mark.parametrize("freshness_seconds", [None, 0, 1, 3600, 86400, 172800])
@pytest.mark.parametrize(
    "age_seconds", [0, 1, 3599, 3600, 3601, 86399, 86400, 86401, 10**6]
)
def test_the_copied_rule_agrees_with_the_regime_view_at_every_boundary(
    freshness_seconds, age_seconds
) -> None:
    now = datetime(2026, 11, 11, tzinfo=UTC)
    snapshot = ns(
        snapshot_at=now - timedelta(seconds=age_seconds),
        freshness_seconds=freshness_seconds,
    )
    expected = _crypto_regime._state_for(snapshot, now=now)
    assert (
        crypto_insight_state(snapshot.snapshot_at, freshness_seconds, now) == expected
    )


def test_the_disabled_open_interest_fact_is_the_regime_views_fact() -> None:
    assert CRYPTO_OI_DISABLED is True
    assert re.search(r'"aggregate_oi":\s*\{\s*"state":\s*"disabled"', REGIME_SOURCE), (
        "the regime view no longer reports aggregate_oi as disabled"
    )
