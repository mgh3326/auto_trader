from __future__ import annotations

import datetime as dt
import json
import sqlite3

import httpx
import pytest

from app.services.brokers.binance import r4_p0_collector as collector_module
from app.services.brokers.binance.r4_p0_collector import (
    AppendOnlyPITStore,
    BinanceR4P0Collector,
    CollectorConfig,
)
from app.services.brokers.binance.r4_p0_hardening import (
    FINAL_COMPLETE,
    FINAL_CONFLICT,
    FINAL_MISSING,
    AlertDispatcher,
    DeterministicEpochFinalizer,
    EpochLedger,
    EpochPolicy,
    RawPITReader,
    RawPITReadError,
    availability_report,
    decision_epoch_for_event,
    finalization_report,
    finalize_at,
)

EPOCH = dt.datetime(2026, 7, 28, 8, tzinfo=dt.UTC)
RECEIVED = dt.datetime(2026, 7, 28, 7, 1, tzinfo=dt.UTC)
POLICY = EpochPolicy(
    required_sources=(
        "binance_usdm.openInterestHist",
        "binance_usdm.takerLongShortRatio",
    ),
    symbols=("XRPUSDT",),
)


def _payload(
    source: str,
    value: str = "10",
    *,
    timestamp: int = 1785222000000,
) -> dict[str, str]:
    if source == "binance_usdm.openInterestHist":
        return {
            "sumOpenInterest": value,
            "symbol": "XRPUSDT",
            "timestamp": str(timestamp),
        }
    if source == "binance_usdm.basis":
        return {
            "basis": value,
            "contractType": "PERPETUAL",
            "pair": "XRPUSDT",
            "timestamp": str(timestamp),
        }
    return {
        "buySellRatio": value,
        "buyVol": "20",
        "sellVol": "2",
        "symbol": "XRPUSDT",
        "timestamp": str(timestamp),
    }


def _append(
    store: AppendOnlyPITStore,
    source: str,
    *,
    value: str = "10",
    received: dt.datetime = RECEIVED,
    sequence: str = "1785222000000",
    event_time: dt.datetime = dt.datetime(2026, 7, 28, 7, tzinfo=dt.UTC),
) -> None:
    timestamp = int(event_time.timestamp() * 1000)
    assert store.append(
        source=source,
        symbol="XRPUSDT",
        raw_payload=_payload(source, value, timestamp=timestamp),
        local_receive_time=received,
        run_id="replica:test",
        event_time=event_time.isoformat().replace("+00:00", "Z"),
        transaction_time=None,
        request_started_at="2026-07-28T07:00:59.000000Z",
        request_completed_at="2026-07-28T07:01:00.000000Z",
        sequence_or_trade_id=sequence,
        gap_detected=False,
        reconnect_id="replica:test:rest",
    )


def _append_complete_periodic_source(
    store: AppendOnlyPITStore,
    source: str,
    *,
    observations: int = 48,
) -> None:
    interval_start = EPOCH - dt.timedelta(hours=4)
    for index in range(observations):
        event_time = interval_start + dt.timedelta(minutes=5 * index)
        _append(
            store,
            source,
            sequence=str(int(event_time.timestamp() * 1000)),
            event_time=event_time,
        )


def _append_agg(store: AppendOnlyPITStore, *, gap: bool) -> None:
    assert store.append(
        source="binance_usdm.aggTrade",
        symbol="XRPUSDT",
        raw_payload={
            "E": 1785222000000,
            "T": 1785222000000,
            "a": 42,
            "p": "1.25",
            "q": "3",
            "s": "XRPUSDT",
        },
        local_receive_time=RECEIVED,
        run_id=f"replica:gap:{gap}",
        event_time="2026-07-28T07:00:00.000000Z",
        transaction_time="2026-07-28T07:00:00.000000Z",
        request_started_at="2026-07-28T06:59:59.000000Z",
        request_completed_at="2026-07-28T07:00:00.000000Z",
        sequence_or_trade_id="42",
        gap_detected=gap,
        reconnect_id=f"replica:gap:{gap}:ws",
    )


def _finalizer(
    store: AppendOnlyPITStore,
    *,
    policy: EpochPolicy = POLICY,
    replicas: tuple = (),
) -> tuple[EpochLedger, DeterministicEpochFinalizer]:
    ledger = EpochLedger(store._db, policy)  # noqa: SLF001
    reader = RawPITReader(store._db, store.path, replicas)  # noqa: SLF001
    return ledger, DeterministicEpochFinalizer(ledger, reader)


@pytest.mark.unit
def test_decision_epoch_uses_half_open_boundary() -> None:
    assert (
        decision_epoch_for_event(dt.datetime(2026, 7, 28, 7, 59, 59, tzinfo=dt.UTC))
        == EPOCH
    )
    assert decision_epoch_for_event(EPOCH) == EPOCH + dt.timedelta(hours=4)
    assert finalize_at(EPOCH) == dt.datetime(2026, 7, 28, 12, tzinfo=dt.UTC)


@pytest.mark.unit
def test_finalizer_is_deterministic_across_input_order(tmp_path) -> None:
    hashes: list[str] = []
    for index, sources in enumerate(
        (
            POLICY.required_sources,
            tuple(reversed(POLICY.required_sources)),
        )
    ):
        root = tmp_path / str(index)
        with AppendOnlyPITStore(root) as store:
            for source in sources:
                _append_complete_periodic_source(store, source)
            _, finalizer = _finalizer(store)
            preview = finalizer.preview("XRPUSDT", EPOCH)
            result = finalizer.finalize(
                "XRPUSDT",
                EPOCH,
                observed_at=finalize_at(EPOCH),
            )
            repeated = finalizer.finalize(
                "XRPUSDT",
                EPOCH,
                observed_at=finalize_at(EPOCH) + dt.timedelta(minutes=1),
            )
            assert preview.final_status == FINAL_COMPLETE
            assert result.inserted
            assert not repeated.inserted
            assert result.evaluation_hash == repeated.evaluation_hash
            hashes.append(result.evaluation_hash)
    assert hashes[0] == hashes[1]


@pytest.mark.unit
def test_finalizer_missing_and_conflict_are_fail_closed(tmp_path) -> None:
    missing_root = tmp_path / "missing"
    with AppendOnlyPITStore(missing_root) as store:
        _append_complete_periodic_source(store, POLICY.required_sources[0])
        _, finalizer = _finalizer(store)
        result = finalizer.finalize("XRPUSDT", EPOCH, observed_at=finalize_at(EPOCH))
        assert result.final_status == FINAL_MISSING
        assert result.missing_sources == (POLICY.required_sources[1],)

    conflict_root = tmp_path / "conflict"
    with AppendOnlyPITStore(conflict_root) as store:
        for source in POLICY.required_sources:
            _append_complete_periodic_source(store, source)
        _append(
            store,
            POLICY.required_sources[0],
            value="11",
            sequence="1785222000000",
        )
        _, finalizer = _finalizer(store)
        result = finalizer.finalize("XRPUSDT", EPOCH, observed_at=finalize_at(EPOCH))
        assert result.final_status == FINAL_CONFLICT
        assert result.conflict_sources == (POLICY.required_sources[0],)


@pytest.mark.unit
@pytest.mark.parametrize("observations", [1, 24, 47, 48])
def test_periodic_source_requires_full_epoch_coverage(tmp_path, observations) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
    )
    with AppendOnlyPITStore(tmp_path / str(observations)) as store:
        _append_complete_periodic_source(
            store,
            "binance_usdm.basis",
            observations=observations,
        )
        _, finalizer = _finalizer(store, policy=policy)
        preview = finalizer.preview("XRPUSDT", EPOCH)

        if observations == 48:
            assert preview.final_status == FINAL_COMPLETE
            assert preview.invalid_sources == ()
        else:
            assert preview.final_status == FINAL_MISSING
            assert preview.missing_sources == ()
            assert preview.invalid_sources == ("binance_usdm.basis",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_periodic_coverage_triggers_historical_retry(
    tmp_path,
    monkeypatch,
) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
    )
    with AppendOnlyPITStore(tmp_path) as store:
        _append_complete_periodic_source(
            store,
            "binance_usdm.basis",
            observations=24,
        )
        _, finalizer = _finalizer(store, policy=policy)
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path,
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
            ),
            store,
        )
        collector.epoch_policy = policy
        collector.epoch_finalizer = finalizer
        calls: list[tuple[str, str]] = []

        async def retry_history(
            _client,
            *,
            source: str,
            symbol: str,
            decision_epoch: dt.datetime,
        ) -> None:
            assert decision_epoch == EPOCH
            calls.append((source, symbol))

        monkeypatch.setattr(collector, "_rest_epoch_history", retry_history)
        async with httpx.AsyncClient() as client:
            await collector._retry_incomplete_epoch(  # noqa: SLF001
                client,
                EPOCH,
                EPOCH + dt.timedelta(hours=1),
            )

        assert calls == [("binance_usdm.basis", "XRPUSDT")]


@pytest.mark.unit
def test_independent_replica_covers_same_identity_gap(tmp_path) -> None:
    gap_policy = EpochPolicy(
        required_sources=("binance_usdm.aggTrade",),
        symbols=("XRPUSDT",),
    )
    with (
        AppendOnlyPITStore(tmp_path / "gap") as gap_store,
        AppendOnlyPITStore(tmp_path / "continuous") as continuous_store,
    ):
        _append_agg(gap_store, gap=True)
        _append_agg(continuous_store, gap=False)
        _, local = _finalizer(gap_store, policy=gap_policy)
        assert local.preview("XRPUSDT", EPOCH).final_status == FINAL_MISSING
        _, merged = _finalizer(
            gap_store,
            policy=gap_policy,
            replicas=(continuous_store.path,),
        )
        assert merged.preview("XRPUSDT", EPOCH).final_status == FINAL_COMPLETE


@pytest.mark.unit
def test_finalization_ignores_late_payload_and_records_late_only(tmp_path) -> None:
    with AppendOnlyPITStore(tmp_path) as store:
        for source in POLICY.required_sources:
            _append_complete_periodic_source(store, source)
        ledger, finalizer = _finalizer(store)
        original = finalizer.finalize("XRPUSDT", EPOCH, observed_at=finalize_at(EPOCH))
        _append(
            store,
            POLICY.required_sources[0],
            value="999",
            received=finalize_at(EPOCH) + dt.timedelta(seconds=1),
            sequence="1785222000000",
        )
        repeated = finalizer.finalize(
            "XRPUSDT",
            EPOCH,
            observed_at=finalize_at(EPOCH) + dt.timedelta(minutes=1),
        )
        assert repeated.final_status == FINAL_COMPLETE
        assert repeated.evaluation_hash == original.evaluation_hash
        assert (
            finalizer.append_late_only(
                "XRPUSDT",
                EPOCH,
                recorded_at=finalize_at(EPOCH) + dt.timedelta(minutes=1),
            )
            == 1
        )
        row = ledger.db.execute(
            "SELECT correction_status FROM late_only_corrections"
        ).fetchone()
        assert row["correction_status"] == "LATE_ONLY"


@pytest.mark.unit
def test_attempt_and_finalization_tables_reject_update_delete(tmp_path) -> None:
    with AppendOnlyPITStore(tmp_path) as store:
        for source in POLICY.required_sources:
            _append_complete_periodic_source(store, source)
        ledger, finalizer = _finalizer(store)
        ledger.ensure_open_rows(EPOCH, "replica-a", RECEIVED)
        ledger.append_attempt(
            collector_instance_id="replica-a",
            decision_epoch=EPOCH,
            attempted_at=RECEIVED,
            completed_at=RECEIVED + dt.timedelta(seconds=1),
            request_identity={
                "method": "GET",
                "path": "/futures/data/openInterestHist",
            },
            response_sha256="a" * 64,
            terminal_status="SUCCESS",
        )
        finalizer.finalize("XRPUSDT", EPOCH, observed_at=finalize_at(EPOCH))
        assert (
            ledger.db.execute("SELECT count(*) FROM epoch_source_events").fetchone()[0]
            == 2
        )
        for table in (
            "epoch_source_events",
            "collector_attempt_starts",
            "collector_attempts",
            "symbol_epoch_finalizations",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                ledger.db.execute(f"DELETE FROM {table}")  # noqa: S608
            ledger.db.rollback()
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                ledger.db.execute(
                    f"UPDATE {table} SET append_id = append_id"  # noqa: S608
                )
            ledger.db.rollback()


@pytest.mark.unit
def test_replica_finalizations_are_deduplicated_and_divergence_is_visible(
    tmp_path,
) -> None:
    paths = []
    stores = []
    try:
        for name in ("a", "b"):
            store = AppendOnlyPITStore(tmp_path / name)
            stores.append(store)
            for source in POLICY.required_sources:
                _append_complete_periodic_source(store, source)
            _, finalizer = _finalizer(store)
            finalizer.finalize("XRPUSDT", EPOCH, observed_at=finalize_at(EPOCH))
            paths.append(store.path)
        _, local_only = _finalizer(stores[0])
        _, with_exact_replica = _finalizer(stores[0], replicas=(stores[1].path,))
        assert (
            local_only.preview("XRPUSDT", EPOCH).evaluation_hash
            == with_exact_replica.preview("XRPUSDT", EPOCH).evaluation_hash
        )
        report = finalization_report(paths, POLICY, decision_epoch=EPOCH)
        assert report["missing_symbols"] == []
        assert report["divergent_symbols"] == []
        assert len(report["finalizations"]) == 1
    finally:
        for store in stores:
            store.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_historical_retry_appends_terminal_attempt(tmp_path) -> None:
    timestamp = int(dt.datetime(2026, 7, 28, 7, tzinfo=dt.UTC).timestamp() * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/futures/data/openInterestHist"
        assert request.url.params["startTime"] == str(
            int((EPOCH - dt.timedelta(hours=4)).timestamp() * 1000)
        )
        assert request.url.params["endTime"] == str(int(EPOCH.timestamp() * 1000) - 1)
        return httpx.Response(
            200,
            json=[
                {
                    "sumOpenInterest": "10",
                    "symbol": "XRPUSDT",
                    "timestamp": timestamp,
                }
            ],
        )

    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path,
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
            ),
            store,
        )
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            await collector._rest_epoch_history(  # noqa: SLF001
                client,
                source="binance_usdm.openInterestHist",
                symbol="XRPUSDT",
                decision_epoch=EPOCH,
            )
        attempt = store._db.execute(  # noqa: SLF001
            "SELECT * FROM collector_attempts"
        ).fetchone()
        start = store._db.execute(  # noqa: SLF001
            "SELECT * FROM collector_attempt_starts"
        ).fetchone()
        assert start["attempt_id"] == attempt["attempt_id"]
        assert attempt["terminal_status"] == "SUCCESS"
        assert attempt["response_sha256"]
        identity = json.loads(attempt["request_identity_json"])
        assert identity["method"] == "GET"
        assert identity["sources"] == ["binance_usdm.openInterestHist"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_taker_history_shift_restores_full_epoch_coverage(tmp_path) -> None:
    cadence = dt.timedelta(minutes=5)
    interval_start = EPOCH - dt.timedelta(hours=4)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/futures/data/takerlongshortRatio"
        request_start = int(request.url.params["startTime"])
        assert request_start == int((interval_start + cadence).timestamp() * 1000)
        assert request.url.params["endTime"] == str(
            int((EPOCH + cadence).timestamp() * 1000) - 1
        )
        # This endpoint returns the bucket preceding each requested boundary.
        first_bucket = request_start - int(cadence.total_seconds() * 1000)
        return httpx.Response(
            200,
            json=[
                _payload(
                    "binance_usdm.takerLongShortRatio",
                    timestamp=first_bucket
                    + index * int(cadence.total_seconds() * 1000),
                )
                for index in range(48)
            ],
        )

    policy = EpochPolicy(
        required_sources=("binance_usdm.takerLongShortRatio",),
        symbols=("XRPUSDT",),
    )
    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path,
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
            ),
            store,
        )
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            await collector._rest_epoch_history(  # noqa: SLF001
                client,
                source="binance_usdm.takerLongShortRatio",
                symbol="XRPUSDT",
                decision_epoch=EPOCH,
            )

        stored_rows = store._db.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM pit_records "
            "WHERE source = 'binance_usdm.takerLongShortRatio'"
        ).fetchone()[0]
        _, finalizer = _finalizer(store, policy=policy)
        preview = finalizer.preview("XRPUSDT", EPOCH)

        assert stored_rows == 48
        assert preview.final_status == FINAL_COMPLETE
        assert preview.invalid_sources == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_retry_response_keeps_hash_and_terminal_failure(
    tmp_path,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path,
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
            ),
            store,
        )
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(ValueError, match="empty historical"):
                await collector._rest_epoch_history(  # noqa: SLF001
                    client,
                    source="binance_usdm.openInterestHist",
                    symbol="XRPUSDT",
                    decision_epoch=EPOCH,
                )
        attempt = store._db.execute(  # noqa: SLF001
            "SELECT * FROM collector_attempts"
        ).fetchone()
        assert attempt["terminal_status"] == "INVALID_RESPONSE"
        assert attempt["response_sha256"]
        assert attempt["error_type"] == "ValueError"
        assert attempt["error_message"] == (
            "empty historical response from /futures/data/openInterestHist"
        )
        assert "ValueError: empty historical response" in attempt["error_traceback"]
        assert attempt["response_body_summary"] == "[]"


@pytest.mark.unit
def test_rows_for_epoch_surfaces_unreadable_replica(tmp_path) -> None:
    with AppendOnlyPITStore(tmp_path / "local") as store:
        replica = tmp_path / "replica.sqlite3"
        replica.write_text("copy still in progress", encoding="utf-8")
        reader = RawPITReader(store._db, store.path, (replica,))  # noqa: SLF001

        with pytest.raises(
            RawPITReadError,
            match="failed to read replica PIT artifact.*file is not a database",
        ):
            reader.rows_for_epoch(
                symbol="XRPUSDT",
                decision_epoch=EPOCH,
                received_by=finalize_at(EPOCH),
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_epoch_supervisor_failure_is_logged_counted_and_stops(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path,
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
            ),
            store,
        )
        now = finalize_at(EPOCH)
        monkeypatch.setattr(collector_module, "utc_now", lambda: now)
        monkeypatch.setattr(
            collector_module,
            "scheduled_epochs",
            lambda _start, _end: (EPOCH,),
        )

        async def fail_finalize(
            _epoch: dt.datetime,
            _observed_at: dt.datetime,
        ) -> None:
            raise RuntimeError("injected finalizer failure")

        monkeypatch.setattr(collector, "_finalize_epoch", fail_finalize)
        with (
            caplog.at_level("ERROR", logger="r4_p0_collector"),
            pytest.raises(RuntimeError, match="injected finalizer failure"),
        ):
            await collector._epoch_supervisor()  # noqa: SLF001

        assert collector.stop.is_set()
        assert collector.failure_counts == {"epoch_supervisor": 1}
        record = caplog.records[-1]
        assert "detail='injected finalizer failure'" in record.getMessage()
        assert record.exc_info is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_failures_are_logged_counted_and_preserve_diagnostics(
    tmp_path,
    caplog,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="provider failed: incident-247")

    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path,
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
            ),
            store,
        )
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            with caplog.at_level("ERROR", logger="r4_p0_collector"):
                await collector._retry_incomplete_epoch(  # noqa: SLF001
                    client,
                    EPOCH,
                    EPOCH + dt.timedelta(hours=1),
                )

        attempts = list(
            store._db.execute(  # noqa: SLF001
                """
                SELECT terminal_status, error_type, error_message,
                       error_traceback, response_body_summary
                FROM collector_attempts ORDER BY append_id
                """
            )
        )
        assert len(attempts) == 4
        assert collector.failure_counts == {"epoch_retry": 4}
        assert (
            len(
                [
                    record
                    for record in caplog.records
                    if "collector.epoch_retry.failed" in record.getMessage()
                ]
            )
            == 4
        )
        for attempt in attempts:
            assert attempt["terminal_status"] == "HTTP_ERROR"
            assert attempt["error_type"] == "HTTPStatusError"
            assert "500 Internal Server Error" in attempt["error_message"]
            assert "httpx.HTTPStatusError" in attempt["error_traceback"]
            assert attempt["response_body_summary"] == "provider failed: incident-247"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alert_log_path_and_two_replica_health(tmp_path) -> None:
    paths = []
    stores = []
    try:
        for name in ("a", "b"):
            root = tmp_path / name
            store = AppendOnlyPITStore(root)
            stores.append(store)
            ledger = EpochLedger(store._db, POLICY)  # noqa: SLF001
            ledger.append_heartbeat(
                collector_instance_id=f"replica-{name}",
                run_id=f"run-{name}",
                observed_at=RECEIVED,
                health={"ok": True},
            )
            paths.append(store.path)
        report = availability_report(
            paths,
            POLICY,
            observed_at=RECEIVED + dt.timedelta(seconds=30),
            stale_after_seconds=60,
        )
        assert report["healthy_replica_count"] == 2

        ledger = EpochLedger(stores[0]._db, POLICY)  # noqa: SLF001
        dispatcher = AlertDispatcher(ledger)
        await dispatcher.emit(
            alert_key="DATA_INTEGRITY_FAIL:test",
            severity="CRITICAL",
            payload={"alert_type": "DATA_INTEGRITY_FAIL"},
            now=RECEIVED,
        )
        await dispatcher.emit(
            alert_key="DATA_INTEGRITY_FAIL:test",
            severity="CRITICAL",
            payload={"alert_type": "DATA_INTEGRITY_FAIL"},
            now=RECEIVED,
        )
        events = list(
            ledger.db.execute(
                "SELECT event_type, delivery_status "
                "FROM collector_alert_events ORDER BY append_id"
            )
        )
        assert [tuple(row) for row in events] == [
            ("ALERT_RAISED", "PERSISTED"),
            ("LOG_DELIVERY", "SUCCEEDED"),
        ]
    finally:
        for store in stores:
            store.close()
