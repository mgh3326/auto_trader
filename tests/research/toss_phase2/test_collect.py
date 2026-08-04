from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pyarrow.parquet as pq
import pytest

from research.toss_phase2.collect import (
    KST,
    STAGING_CONTRACT,
    VALUE_SEMANTICS,
    CachedTokenOnlyProvider,
    CollectionStopped,
    ProductionTossLogMonitor,
    SharedTossHealthMonitor,
    classify_session_segment,
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
        ((20, 1), "UNKNOWN"),
    ],
)
def test_session_segment_is_clock_time_only(clock, expected) -> None:
    timestamp = datetime(2026, 8, 3, *clock, tzinfo=KST)
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
async def test_shared_monitor_stops_on_new_error() -> None:
    class Signal:
        def __init__(self, sequence: int) -> None:
            self.sequence = sequence
            self.status_code = 429
            self.error_type = "http_response"
            self.error_code = "too-many-requests"

    signal: Signal | None = Signal(3)

    async def read() -> Signal | None:
        return signal

    monitor = SharedTossHealthMonitor(read)
    await monitor.start()
    signal = Signal(4)
    with pytest.raises(CollectionStopped, match="shared_toss_error_observed"):
        await monitor.assert_healthy()


@pytest.mark.asyncio
async def test_production_log_monitor_stops_only_for_new_toss_error(tmp_path) -> None:
    log = tmp_path / "production.log"
    log.write_text("old Toss API error status=429\n")
    monitor = ProductionTossLogMonitor([log])
    await monitor.start()

    with log.open("a") as fh:
        fh.write("ordinary startup message\n")
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
