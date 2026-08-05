from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pyarrow.parquet as pq
import pytest

from research.toss_phase2.collect import (
    CALL_BUDGET_ACCOUNTING,
    KST,
    STAGING_CONTRACT,
    VALUE_SEMANTICS,
    CachedTokenOnlyProvider,
    CollectionStopped,
    ProductionTossLogMonitor,
    SharedTossHealthMonitor,
    UnclassifiableSessionSegment,
    classify_session_segment,
    collection_stats_from_checkpoint,
    cumulative_calls_from_progress,
    initialize_staging,
    load_readonly_toss_environment,
    make_rows,
    resolve_toss_base_url,
    write_page,
)


def test_empty_configured_base_url_uses_toss_transport_default() -> None:
    """The scoped env intentionally omits TOSS_API_BASE_URL."""
    from app.services.brokers.toss.transport import DEFAULT_TOSS_BASE_URL

    assert resolve_toss_base_url("", DEFAULT_TOSS_BASE_URL) == DEFAULT_TOSS_BASE_URL


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ((8, 0), "NXT_PRE"),
        ((8, 59), "NXT_PRE"),
        ((9, 0), "KRX_REGULAR"),
        ((15, 30), "KRX_REGULAR"),
        ((15, 31), "NXT_POST"),
        ((20, 0), "NXT_POST"),
        ((20, 1), None),
    ],
)
def test_session_segment_is_clock_time_only(clock, expected) -> None:
    timestamp = datetime(2026, 8, 3, *clock, tzinfo=KST)
    if expected is None:
        with pytest.raises(UnclassifiableSessionSegment):
            classify_session_segment(timestamp)
    else:
        assert classify_session_segment(timestamp) == expected


@dataclass
class _Candle:
    timestamp: str
    open_price: str
    high_price: str
    low_price: str
    close_price: str
    volume: str


def test_rows_are_staging_shaped_and_exclude_latest_session() -> None:
    eligible = _Candle("2026-08-03T15:30:00+09:00", "10", "12", "9", "11", "0")
    latest = _Candle("2026-08-04T09:00:00+09:00", "10", "12", "9", "11", "5")

    rows = make_rows(
        candles=[eligible, latest],
        symbol="005930",
        start_date=date(2021, 12, 20),
        last_eligible_date=date(2026, 8, 3),
        retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
        batch_id="batch",
        official_nxt_launch_date=None,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["session_segment"] == "KRX_REGULAR"
    assert row["value"] == 0.0
    assert row["value_semantics"] == VALUE_SEMANTICS
    assert row["is_padding"] is True
    assert row["pre_nxt"] is None
    assert row["time_utc"] == datetime(2026, 8, 3, 6, 30, tzinfo=UTC)


def test_write_page_is_immutable_and_labels_parquet(tmp_path) -> None:
    row = {
        "time_utc": datetime(2026, 8, 3, tzinfo=UTC),
        "session_date_kst": date(2026, 8, 3),
        "symbol": "005930",
        "session_segment": "NXT_PRE",
        "source": "TOSS",
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "volume": 0.0,
        "value": 0.0,
        "value_semantics": VALUE_SEMANTICS,
        "is_padding": True,
        "pre_nxt": None,
        "retrieved_at": datetime(2026, 8, 4, tzinfo=UTC),
        "batch_id": "batch",
    }
    path, reused = write_page(
        staging_dir=tmp_path,
        symbol="005930",
        request_before=None,
        next_before="cursor-1",
        rows=[row],
        batch_id="batch",
    )
    assert path is not None
    assert reused is False
    metadata = pq.read_metadata(path).metadata
    assert metadata is not None
    assert metadata[b"artifact_state"] == STAGING_CONTRACT.encode()
    assert metadata[b"next_before"] == b"cursor-1"

    same_path, reused = write_page(
        staging_dir=tmp_path,
        symbol="005930",
        request_before=None,
        next_before="different-cursor-must-not-overwrite",
        rows=[{**row, "close": 99.0}],
        batch_id="batch",
    )
    assert same_path == path
    assert reused is True
    assert pq.read_table(path).column("close")[0].as_py() == 1.0


def test_existing_staging_manifest_replaces_only_withdrawn_pacing_metadata(
    tmp_path,
) -> None:
    immutable = {
        "artifact_state": STAGING_CONTRACT,
        "scope": "top-200 x 4.6y",
        "symbol_count": 200,
        "universe_sha256": "sealed-universe",
        "window": {"start_date": "2021-12-20", "end_date": "2026-08-03"},
        "call_budget_declared": 820000,
        "batch_id": "batch",
    }
    legacy_manifest = {
        **immutable,
        "pacing": {"during_09_00_to_20_00_kst_seconds": 0.5},
        "preserve": "staging-data-contract",
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(legacy_manifest))
    page = tmp_path / "data" / "symbol=005930" / "part-existing.parquet"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"existing-page-must-not-change")

    initialize_staging(
        staging_dir=tmp_path,
        manifest={
            **immutable,
            "rate_limit_control": {"mode": "response_header_adaptive"},
        },
    )

    updated = json.loads(manifest_path.read_text())
    assert "pacing" not in updated
    assert updated["rate_limit_control"] == {"mode": "response_header_adaptive"}
    assert updated["preserve"] == "staging-data-contract"
    assert page.read_bytes() == b"existing-page-must-not-change"


def test_resumed_summary_is_seeded_from_checkpoint_without_rewriting_pages(
    tmp_path,
) -> None:
    checkpoint_path = tmp_path / "state" / "checkpoint.json"
    checkpoint_path.parent.mkdir()
    checkpoint_path.write_text(
        json.dumps(
            {
                "version": 1,
                "symbols": {
                    "005930": {
                        "before": None,
                        "done": True,
                        "pages": 12,
                        "rows_staged": 2398,
                    },
                    "000660": {
                        "before": "cursor",
                        "done": False,
                        "pages": 3,
                        "rows_staged": 600,
                    },
                },
            }
        )
    )

    stats = collection_stats_from_checkpoint(
        staging_dir=tmp_path,
        symbols=["005930", "000660"],
    )

    assert stats.symbols_total == 2
    assert stats.symbols_done == 1
    assert stats.pages_staged == 15
    assert stats.rows_staged == 2998


def test_call_budget_is_cumulative_across_legacy_and_resumed_processes(
    tmp_path,
) -> None:
    progress = tmp_path / "events" / "progress.jsonl"
    progress.parent.mkdir()
    events = [
        {"event": "collection_started"},
        {"event": "page", "calls_actual": 125},
        {"event": "collection_started"},
        {"event": "page", "calls_actual": 201},
        {
            "event": "collection_started",
            "call_accounting": CALL_BUDGET_ACCOUNTING,
            "prior_calls_actual": 326,
        },
        {"event": "page", "calls_actual": 330},
    ]
    progress.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    assert cumulative_calls_from_progress(progress) == 330


@pytest.mark.asyncio
async def test_cached_token_provider_never_issues_or_forces() -> None:
    class Manager:
        async def get_cached_access_token(self) -> str | None:
            return "cached-token"

    provider = CachedTokenOnlyProvider(Manager())
    assert await provider.get_access_token() == "cached-token"
    with pytest.raises(CollectionStopped, match="force_reissue_prohibited"):
        await provider.get_access_token(force_reissue=True)


@pytest.mark.asyncio
async def test_shared_monitor_treats_429_as_recoverable_but_stops_other_errors() -> (
    None
):
    class Signal:
        def __init__(self, sequence: int, status_code: int) -> None:
            self.sequence = sequence
            self.status_code = status_code
            self.error_type = "http_response"
            self.error_code = "too-many-requests"

    signal: Signal | None = Signal(3, 429)

    async def read() -> Signal | None:
        return signal

    monitor = SharedTossHealthMonitor(read)
    await monitor.start()
    signal = Signal(4, 429)
    await monitor.assert_healthy()
    signal = Signal(5, 500)
    with pytest.raises(CollectionStopped, match="shared_toss_error_observed"):
        await monitor.assert_healthy()


@pytest.mark.asyncio
async def test_production_log_monitor_treats_new_429_as_recoverable(tmp_path) -> None:
    log = tmp_path / "production.log"
    log.write_text("old Toss API error status=429\n")
    monitor = ProductionTossLogMonitor([log])
    await monitor.start()

    with log.open("a") as fh:
        fh.write("ordinary startup message\n")
    await monitor.assert_healthy()

    with log.open("a") as fh:
        fh.write("Toss API error status=429 code=rate-limit-exceeded\n")
    await monitor.assert_healthy()

    with log.open("a") as fh:
        fh.write("Toss API error status=401 code=invalid-token\n")
    with pytest.raises(CollectionStopped, match="production_toss_error_log_observed"):
        await monitor.assert_healthy()


def test_readonly_env_loads_only_toss_and_inert_settings(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env.toss-data-readonly.native"
    env_file.write_text(
        "TOSS_API_ENABLED=true\n"
        "TOSS_API_CLIENT_ID=test-client\n"
        "TOSS_API_CLIENT_SECRET=test-secret\n"
    )
    for name in (
        "ENV_FILE",
        "REDIS_URL",
        "TOSS_LIVE_ORDER_MUTATIONS_ENABLED",
        "TOSS_API_ENABLED",
        "TOSS_API_CLIENT_ID",
        "TOSS_API_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    source = load_readonly_toss_environment(env_file)

    assert source.startswith("defaulted:")
    assert "test-secret" not in source
    assert "prod" not in str(os.environ["ENV_FILE"])
