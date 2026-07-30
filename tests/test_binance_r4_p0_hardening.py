from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import Counter

import httpx
import pytest

from app.services.brokers.binance import r4_p0_collector as collector_module
from app.services.brokers.binance.r4_p0_collector import (
    AppendOnlyPITStore,
    BasisPollError,
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
    T0StartupGateError,
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
    symbol: str = "XRPUSDT",
) -> dict[str, str]:
    if source == "binance_usdm.openInterestHist":
        return {
            "sumOpenInterest": value,
            "symbol": symbol,
            "timestamp": str(timestamp),
        }
    if source == "binance_usdm.basis":
        return {
            "basis": value,
            "contractType": "PERPETUAL",
            "pair": symbol,
            "timestamp": str(timestamp),
        }
    return {
        "buySellRatio": value,
        "buyVol": "20",
        "sellVol": "2",
        "symbol": symbol,
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
    symbol: str = "XRPUSDT",
) -> None:
    timestamp = int(event_time.timestamp() * 1000)
    assert store.append(
        source=source,
        symbol=symbol,
        raw_payload=_payload(source, value, timestamp=timestamp, symbol=symbol),
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
    symbol: str = "XRPUSDT",
) -> None:
    interval_start = EPOCH - dt.timedelta(hours=4)
    for index in range(observations):
        event_time = interval_start + dt.timedelta(minutes=5 * index)
        _append(
            store,
            source,
            sequence=str(int(event_time.timestamp() * 1000)),
            event_time=event_time,
            symbol=symbol,
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


def _configure_collector_policy(
    collector: BinanceR4P0Collector,
    store: AppendOnlyPITStore,
    policy: EpochPolicy,
    *,
    replicas: tuple = (),
) -> tuple[DeterministicEpochFinalizer, DeterministicEpochFinalizer]:
    ledger = EpochLedger(store._db, policy)  # noqa: SLF001
    union_reader = RawPITReader(store._db, store.path, replicas)  # noqa: SLF001
    local_reader = RawPITReader(store._db, store.path, ())  # noqa: SLF001
    union_finalizer = DeterministicEpochFinalizer(ledger, union_reader)
    local_finalizer = DeterministicEpochFinalizer(ledger, local_reader)
    collector.epoch_policy = policy
    collector.epoch_ledger = ledger
    collector.raw_reader = union_reader
    collector.epoch_finalizer = union_finalizer
    collector.local_raw_reader = local_reader
    collector.local_epoch_finalizer = local_finalizer
    return union_finalizer, local_finalizer


def _append_complete_recoverable_source(
    store: AppendOnlyPITStore,
    source: str,
    symbol: str,
) -> None:
    interval_start = EPOCH - dt.timedelta(hours=4)
    cadence_minutes = 1 if source == "binance_usdm.premiumIndexKline1m" else 5
    observations = 240 if cadence_minutes == 1 else 48
    for index in range(observations):
        event_time = interval_start + dt.timedelta(minutes=cadence_minutes * index)
        timestamp = int(event_time.timestamp() * 1000)
        payload: object
        if source == "binance_usdm.premiumIndexKline1m":
            payload = [
                timestamp - 59_999,
                "1",
                "2",
                "0.5",
                "1.5",
                "3",
                timestamp,
            ]
        else:
            payload = _payload(source, timestamp=timestamp, symbol=symbol)
        assert store.append(
            source=source,
            symbol=symbol,
            raw_payload=payload,
            local_receive_time=EPOCH + dt.timedelta(hours=1),
            run_id="replica:peer",
            event_time=event_time.isoformat().replace("+00:00", "Z"),
            transaction_time=None,
            request_started_at="2026-07-28T08:59:59.000000Z",
            request_completed_at="2026-07-28T09:00:00.000000Z",
            sequence_or_trade_id=str(timestamp),
            gap_detected=False,
            reconnect_id="replica:peer:rest",
        )


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
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path,
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
            ),
            store,
        )
        _configure_collector_policy(collector, store, policy)
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
@pytest.mark.asyncio
async def test_local_47_peer_1_union_complete_still_retries_without_ledger_write(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
    )
    recovery_time = EPOCH + dt.timedelta(hours=1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        missing_time = EPOCH - dt.timedelta(minutes=5)
        return httpx.Response(
            200,
            json=[
                {
                    "basis": "1",
                    "contractType": "PERPETUAL",
                    "pair": "XRPUSDT",
                    "timestamp": int(missing_time.timestamp() * 1000),
                }
            ],
        )

    monkeypatch.setattr(collector_module, "utc_now", lambda: recovery_time)
    with (
        AppendOnlyPITStore(tmp_path / "local") as local_store,
        AppendOnlyPITStore(tmp_path / "peer") as peer_store,
    ):
        _append_complete_periodic_source(
            local_store,
            "binance_usdm.basis",
            observations=47,
        )
        missing_time = EPOCH - dt.timedelta(minutes=5)
        _append(
            peer_store,
            "binance_usdm.basis",
            event_time=missing_time,
            sequence=str(int(missing_time.timestamp() * 1000)),
        )
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path / "local",
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
                replica_artifacts=(peer_store.path,),
            ),
            local_store,
        )
        union_finalizer, local_finalizer = _configure_collector_policy(
            collector,
            local_store,
            policy,
            replicas=(peer_store.path,),
        )
        assert local_finalizer.preview("XRPUSDT", EPOCH).final_status == FINAL_MISSING
        assert union_finalizer.preview("XRPUSDT", EPOCH).final_status == FINAL_COMPLETE
        rows_before = local_store._db.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM symbol_epoch_finalizations"
        ).fetchone()[0]

        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            await collector._retry_incomplete_epoch(  # noqa: SLF001
                client,
                EPOCH,
                recovery_time,
            )

        rows_after = local_store._db.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM symbol_epoch_finalizations"
        ).fetchone()[0]
        assert rows_before == rows_after == 0
        assert len(requests) == 1
        assert requests[0].url.path == "/futures/data/basis"
        assert requests[0].url.params["pair"] == "XRPUSDT"
        assert requests[0].url.params["limit"] == "500"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_production_init_wires_retry_to_local_only_reader(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_time = EPOCH + dt.timedelta(hours=1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        collector_module,
        "REQUIRED_ACTIVE_SOURCES",
        frozenset({"binance_usdm.basis"}),
    )
    monkeypatch.setattr(collector_module, "utc_now", lambda: recovery_time)
    with (
        AppendOnlyPITStore(tmp_path / "local") as local_store,
        AppendOnlyPITStore(tmp_path / "peer") as peer_store,
    ):
        _append_complete_periodic_source(
            local_store,
            "binance_usdm.basis",
            observations=47,
        )
        missing_time = EPOCH - dt.timedelta(minutes=5)
        _append(
            peer_store,
            "binance_usdm.basis",
            event_time=missing_time,
            sequence=str(int(missing_time.timestamp() * 1000)),
        )
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path / "local",
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
                replica_artifacts=(peer_store.path,),
            ),
            local_store,
        )

        assert collector.raw_reader.replica_paths == (peer_store.path.resolve(),)
        assert collector.local_raw_reader.replica_paths == ()
        assert (
            collector.epoch_finalizer.preview("XRPUSDT", EPOCH).final_status
            == FINAL_COMPLETE
        )
        assert (
            collector.local_epoch_finalizer.preview("XRPUSDT", EPOCH).final_status
            == FINAL_MISSING
        )

        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            await collector._retry_incomplete_epoch(  # noqa: SLF001
                client,
                EPOCH,
                recovery_time,
            )

        finalization_rows = local_store._db.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM symbol_epoch_finalizations"
        ).fetchone()[0]
        assert len(requests) == 1
        assert requests[0].url.path == "/futures/data/basis"
        assert requests[0].url.params["pair"] == "XRPUSDT"
        assert requests[0].url.params["limit"] == "500"
        assert finalization_rows == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_local_47_peer_1_finalization_remains_union_complete(tmp_path) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
    )
    with (
        AppendOnlyPITStore(tmp_path / "local") as local_store,
        AppendOnlyPITStore(tmp_path / "peer") as peer_store,
    ):
        _append_complete_periodic_source(
            local_store,
            "binance_usdm.basis",
            observations=47,
        )
        missing_time = EPOCH - dt.timedelta(minutes=5)
        _append(
            peer_store,
            "binance_usdm.basis",
            event_time=missing_time,
            sequence=str(int(missing_time.timestamp() * 1000)),
        )
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path / "local",
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
                replica_artifacts=(peer_store.path,),
            ),
            local_store,
        )
        union_finalizer, _ = _configure_collector_policy(
            collector,
            local_store,
            policy,
            replicas=(peer_store.path,),
        )
        preview = union_finalizer.preview("XRPUSDT", EPOCH)
        assert preview.final_status == FINAL_COMPLETE

        await collector._finalize_epoch(EPOCH, finalize_at(EPOCH))  # noqa: SLF001

        row = local_store._db.execute(  # noqa: SLF001
            """
            SELECT final_status, evaluation_hash
            FROM symbol_epoch_finalizations
            """
        ).fetchone()
        assert row["final_status"] == FINAL_COMPLETE
        assert row["evaluation_hash"] == preview.evaluation_hash
        await collector._alert_deadline_risk(  # noqa: SLF001
            EPOCH,
            finalize_at(EPOCH) - dt.timedelta(minutes=30),
        )
        assert (
            local_store._db.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM collector_alert_events"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bounded_retry_request_count_union_vs_local(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = tuple(
        sorted(
            {
                "binance_usdm.basis",
                "binance_usdm.openInterestHist",
                "binance_usdm.premiumIndexKline1m",
                "binance_usdm.takerLongShortRatio",
            }
        )
    )
    symbols = ("DOGEUSDT", "SOLUSDT", "XRPUSDT")
    policy = EpochPolicy(required_sources=sources, symbols=symbols)
    recovery_time = EPOCH + dt.timedelta(hours=1)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(collector_module, "utc_now", lambda: recovery_time)
    with (
        AppendOnlyPITStore(tmp_path / "local") as local_store,
        AppendOnlyPITStore(tmp_path / "peer") as peer_store,
    ):
        for symbol in symbols:
            for source in sources:
                _append_complete_recoverable_source(peer_store, source, symbol)
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path / "local",
                symbols=symbols,
                collector_instance_id="replica-a",
                replica_artifacts=(peer_store.path,),
            ),
            local_store,
        )
        union_finalizer, local_finalizer = _configure_collector_policy(
            collector,
            local_store,
            policy,
            replicas=(peer_store.path,),
        )
        assert all(
            union_finalizer.preview(symbol, EPOCH).final_status == FINAL_COMPLETE
            for symbol in symbols
        )

        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            collector.local_epoch_finalizer = union_finalizer
            await collector._retry_incomplete_epoch(  # noqa: SLF001
                client,
                EPOCH,
                recovery_time,
            )
            union_request_count = len(paths)

            collector.local_epoch_finalizer = local_finalizer
            await collector._retry_incomplete_epoch(  # noqa: SLF001
                client,
                EPOCH,
                recovery_time,
            )
            local_request_count = len(paths) - union_request_count

        assert union_request_count == 0
        assert local_request_count == len(sources) * len(symbols) == 12
        assert Counter(paths) == {
            "/fapi/v1/premiumIndexKlines": 3,
            "/futures/data/basis": 3,
            "/futures/data/openInterestHist": 3,
            "/futures/data/takerlongshortRatio": 3,
        }
        measurement = {
            "cycle_seconds": 30,
            "union_requests_per_cycle": union_request_count,
            "local_requests_per_cycle": local_request_count,
            "increase_per_cycle": local_request_count - union_request_count,
            "local_requests_per_hour": local_request_count * 120,
            "local_requests_per_4h": local_request_count * 480,
        }
        assert measurement == {
            "cycle_seconds": 30,
            "union_requests_per_cycle": 0,
            "local_requests_per_cycle": 12,
            "increase_per_cycle": 12,
            "local_requests_per_hour": 1440,
            "local_requests_per_4h": 5760,
        }
        print(json.dumps(measurement, sort_keys=True))


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected_exception", "terminal_status"),
    (
        ("empty", BasisPollError, "INVALID_RESPONSE"),
        ("http", httpx.HTTPStatusError, "HTTP_ERROR"),
    ),
)
async def test_basis_failure_recovers_only_inside_epoch_contract_window(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_exception: type[Exception],
    terminal_status: str,
) -> None:
    recovery_time = EPOCH + dt.timedelta(hours=1)
    clock = iter((RECEIVED, RECEIVED, recovery_time, recovery_time))
    monkeypatch.setattr(collector_module, "utc_now", lambda: next(clock))
    interval_start = EPOCH - dt.timedelta(hours=4)
    calls = 0

    def basis_row(event_time: dt.datetime) -> dict[str, object]:
        return {
            "basis": str(event_time.timestamp()),
            "contractType": "PERPETUAL",
            "pair": "XRPUSDT",
            "timestamp": int(event_time.timestamp() * 1000),
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "startTime" not in request.url.params
            if failure_kind == "empty":
                return httpx.Response(200, json=[])
            return httpx.Response(503, text="provider unavailable")

        assert request.url.params["startTime"] == str(
            int(interval_start.timestamp() * 1000)
        )
        assert request.url.params["endTime"] == str(int(EPOCH.timestamp() * 1000) - 1)
        return httpx.Response(
            200,
            json=[
                basis_row(interval_start - dt.timedelta(minutes=5)),
                *[
                    basis_row(interval_start + dt.timedelta(minutes=5 * index))
                    for index in range(48)
                ],
                basis_row(EPOCH),
            ],
        )

    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
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
        finalizer, _ = _configure_collector_policy(collector, store, policy)
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(expected_exception):
                await collector._poll_family(client, "basis")  # noqa: SLF001
            await collector._retry_incomplete_epoch(  # noqa: SLF001
                client,
                EPOCH,
                recovery_time,
            )

        rows = list(
            store._db.execute(  # noqa: SLF001
                """
                SELECT event_time
                FROM pit_records
                WHERE source = 'binance_usdm.basis'
                ORDER BY event_time
                """
            )
        )
        assert len(rows) == 48
        assert rows[0]["event_time"] == "2026-07-28T04:00:00.000000Z"
        assert rows[-1]["event_time"] == "2026-07-28T07:55:00.000000Z"
        assert finalizer.preview("XRPUSDT", EPOCH).final_status == FINAL_COMPLETE
        statuses = [
            row["terminal_status"]
            for row in store._db.execute(  # noqa: SLF001
                "SELECT terminal_status FROM collector_attempts ORDER BY append_id"
            )
        ]
        assert statuses == [terminal_status, "SUCCESS"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basis_recovery_never_starts_at_or_after_deadline(tmp_path) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("deadline-expired recovery must not call the provider")

    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path,
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
            ),
            store,
        )
        _, finalizer = _configure_collector_policy(collector, store, policy)
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            await collector._retry_incomplete_epoch(  # noqa: SLF001
                client,
                EPOCH,
                finalize_at(EPOCH),
            )

        attempt = store._db.execute(  # noqa: SLF001
            "SELECT terminal_status, request_identity_json FROM collector_attempts"
        ).fetchone()
        assert attempt["terminal_status"] == "DEADLINE_EXPIRED"
        assert json.loads(attempt["request_identity_json"]) == {
            "method": "GET",
            "source_name": "binance_usdm.basis",
            "symbol": "XRPUSDT",
            "target": "deadline-recovery",
        }


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
@pytest.mark.asyncio
async def test_production_finalize_records_peer_late_payload_from_union_reader(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        collector_module,
        "REQUIRED_ACTIVE_SOURCES",
        frozenset({"binance_usdm.basis"}),
    )
    with (
        AppendOnlyPITStore(tmp_path / "local") as local_store,
        AppendOnlyPITStore(tmp_path / "peer") as peer_store,
    ):
        _append_complete_periodic_source(local_store, "binance_usdm.basis")
        _append(
            peer_store,
            "binance_usdm.basis",
            received=finalize_at(EPOCH) + dt.timedelta(seconds=1),
            event_time=EPOCH - dt.timedelta(minutes=5),
            sequence="peer-late-only",
        )
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path / "local",
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
                replica_artifacts=(peer_store.path,),
            ),
            local_store,
        )

        await collector._finalize_epoch(EPOCH, finalize_at(EPOCH))  # noqa: SLF001

        correction = local_store._db.execute(  # noqa: SLF001
            """
            SELECT correction_status, raw_record_id
            FROM late_only_corrections
            """
        ).fetchone()
        peer_record_id = peer_store._db.execute(  # noqa: SLF001
            "SELECT record_id FROM pit_records"
        ).fetchone()["record_id"]
        assert correction["correction_status"] == "LATE_ONLY"
        assert correction["raw_record_id"] == peer_record_id


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
async def test_taker_history_shift_restores_full_epoch_coverage(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cadence = dt.timedelta(minutes=5)
    interval_start = EPOCH - dt.timedelta(hours=4)
    monkeypatch.setattr(
        collector_module,
        "utc_now",
        lambda: EPOCH + dt.timedelta(minutes=1),
    )

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
async def test_collector_start_stamps_runtime_code_hash(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code_hash = "a" * 40
    started_at = dt.datetime(2026, 7, 26, 20, tzinfo=dt.UTC)
    monkeypatch.setattr(collector_module, "runtime_code_hash", lambda: code_hash)
    monkeypatch.setattr(collector_module, "utc_now", lambda: started_at)

    with AppendOnlyPITStore(tmp_path) as store:
        _append(store, "binance_usdm.openInterestHist")
        collector = BinanceR4P0Collector(
            CollectorConfig(
                artifact_root=tmp_path,
                symbols=("XRPUSDT",),
                collector_instance_id="replica-a",
            ),
            store,
        )
        collector.stop.set()
        await collector.run()

        stamp = store._db.execute(  # noqa: SLF001
            "SELECT * FROM collector_process_versions"
        ).fetchone()
        assert stamp["collector_instance_id"] == "replica-a"
        assert stamp["run_id"] == collector.run_id
        assert stamp["started_at"] == "2026-07-26T20:00:00.000000Z"
        assert stamp["code_hash"] == code_hash
        assert stamp["collector_version"] == collector_module.COLLECTOR_VERSION
        assert stamp["t0_utc"] == "2026-07-27T00:00:00.000000Z"
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collector_process_versions is append-only",
        ):
            store._db.execute(  # noqa: SLF001
                "UPDATE collector_process_versions SET t0_utc = NULL"
            )
        store._db.rollback()  # noqa: SLF001


@pytest.mark.unit
def test_t0_gate_a_rejects_retroactive_change_with_both_values(tmp_path) -> None:
    original_policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
        study_id="study-t0-a",
        policy_hash="policy-t0-a",
        t0=EPOCH,
    )
    changed_policy = EpochPolicy(
        required_sources=original_policy.required_sources,
        symbols=original_policy.symbols,
        study_id=original_policy.study_id,
        policy_hash=original_policy.policy_hash,
        t0=EPOCH + dt.timedelta(hours=4),
    )
    with AppendOnlyPITStore(tmp_path) as store:
        ledger = EpochLedger(store._db, original_policy)  # noqa: SLF001
        ledger.append_process_version(
            collector_instance_id="replica-a",
            run_id="run-a",
            started_at=EPOCH - dt.timedelta(hours=8),
            code_hash="a" * 40,
            collector_version=collector_module.COLLECTOR_VERSION,
        )
        changed_ledger = EpochLedger(store._db, changed_policy)  # noqa: SLF001

        with pytest.raises(T0StartupGateError) as error:
            changed_ledger.validate_t0_startup(started_at=EPOCH - dt.timedelta(hours=8))

        assert "stored_t0=2026-07-28T08:00:00.000000Z" in str(error.value)
        assert "configured_t0=2026-07-28T12:00:00.000000Z" in str(error.value)


@pytest.mark.unit
def test_t0_gate_a_checks_every_stamp_not_only_latest(tmp_path) -> None:
    original_policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
        study_id="study-t0-all-stamps",
        policy_hash="policy-t0-all-stamps",
        t0=EPOCH,
    )
    current_policy = EpochPolicy(
        required_sources=original_policy.required_sources,
        symbols=original_policy.symbols,
        study_id=original_policy.study_id,
        policy_hash=original_policy.policy_hash,
        t0=EPOCH + dt.timedelta(hours=4),
    )
    with AppendOnlyPITStore(tmp_path) as store:
        EpochLedger(store._db, original_policy).append_process_version(  # noqa: SLF001
            collector_instance_id="replica-a",
            run_id="older-mismatched-run",
            started_at=EPOCH - dt.timedelta(hours=8),
            code_hash="a" * 40,
            collector_version=collector_module.COLLECTOR_VERSION,
        )
        current_ledger = EpochLedger(store._db, current_policy)  # noqa: SLF001
        current_ledger.append_process_version(
            collector_instance_id="replica-a",
            run_id="latest-matching-run",
            started_at=EPOCH - dt.timedelta(hours=7),
            code_hash="a" * 40,
            collector_version=collector_module.COLLECTOR_VERSION,
        )

        with pytest.raises(T0StartupGateError) as error:
            current_ledger.validate_t0_startup(started_at=EPOCH - dt.timedelta(hours=6))

        assert "stored_t0=2026-07-28T08:00:00.000000Z" in str(error.value)
        assert "configured_t0=2026-07-28T12:00:00.000000Z" in str(error.value)


@pytest.mark.unit
def test_t0_gate_a_ignores_legacy_null_stamp(tmp_path) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
        study_id="study-t0-null",
        policy_hash="policy-t0-null",
        t0=EPOCH,
    )
    with AppendOnlyPITStore(tmp_path) as store:
        ledger = EpochLedger(store._db, policy)  # noqa: SLF001
        ledger.db.execute(
            """
            INSERT INTO collector_process_versions (
                version_stamp_id, study_id, policy_hash, collector_instance_id,
                run_id, started_at, code_hash, collector_version, t0_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                "legacy-null",
                policy.study_id,
                policy.policy_hash,
                "replica-a",
                "legacy-run",
                "2026-07-28T00:00:00.000000Z",
                "a" * 40,
                collector_module.COLLECTOR_VERSION,
            ),
        )
        ledger.db.commit()

        ledger.validate_t0_startup(started_at=EPOCH - dt.timedelta(hours=4))


@pytest.mark.unit
def test_t0_gate_b_rejects_late_start_for_fresh_artifact(tmp_path) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
        t0=EPOCH,
    )
    with AppendOnlyPITStore(tmp_path) as store:
        ledger = EpochLedger(store._db, policy)  # noqa: SLF001

        with pytest.raises(
            T0StartupGateError,
            match="기존 T0 를 바꾸지 말고 새 커밋·새 T0 를 사전 고정하라",
        ):
            ledger.validate_t0_startup(
                started_at=EPOCH - dt.timedelta(hours=4) + dt.timedelta(seconds=1)
            )


@pytest.mark.unit
def test_t0_gate_b_allows_timely_start_for_fresh_artifact(tmp_path) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
        t0=EPOCH,
    )
    with AppendOnlyPITStore(tmp_path) as store:
        ledger = EpochLedger(store._db, policy)  # noqa: SLF001

        ledger.validate_t0_startup(started_at=EPOCH - dt.timedelta(hours=4))


@pytest.mark.unit
def test_t0_gate_b_allows_start_before_warmup_cutoff(tmp_path) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
        t0=EPOCH,
    )
    with AppendOnlyPITStore(tmp_path) as store:
        ledger = EpochLedger(store._db, policy)  # noqa: SLF001

        ledger.validate_t0_startup(started_at=EPOCH - dt.timedelta(hours=4, seconds=1))


@pytest.mark.unit
def test_t0_gate_b_allows_late_restart_with_current_identity_stamp(tmp_path) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
        t0=EPOCH,
    )
    with AppendOnlyPITStore(tmp_path) as store:
        ledger = EpochLedger(store._db, policy)  # noqa: SLF001
        ledger.append_process_version(
            collector_instance_id="replica-a",
            run_id="existing-run",
            started_at=EPOCH - dt.timedelta(hours=5),
            code_hash="a" * 40,
            collector_version=collector_module.COLLECTOR_VERSION,
        )

        ledger.validate_t0_startup(started_at=EPOCH + dt.timedelta(days=1))


@pytest.mark.unit
def test_t0_gate_b_allows_legacy_v2_pit_rows_without_any_stamp(tmp_path) -> None:
    policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
        study_id="study-operational-legacy",
        policy_hash="policy-operational-legacy",
        t0=EPOCH,
    )
    with AppendOnlyPITStore(tmp_path) as store:
        _append(store, "binance_usdm.basis")
        ledger = EpochLedger(store._db, policy)  # noqa: SLF001

        ledger.validate_t0_startup(started_at=EPOCH + dt.timedelta(days=1))

        ledger.append_process_version(
            collector_instance_id="replica-a",
            run_id="first-stamped-restart",
            started_at=EPOCH + dt.timedelta(days=1),
            code_hash="a" * 40,
            collector_version=collector_module.COLLECTOR_VERSION,
        )
        ledger.validate_t0_startup(started_at=EPOCH + dt.timedelta(days=2))


@pytest.mark.unit
def test_t0_gate_b_rejects_new_identity_reusing_stale_pit_rows(tmp_path) -> None:
    old_policy = EpochPolicy(
        required_sources=("binance_usdm.basis",),
        symbols=("XRPUSDT",),
        study_id="study-old",
        policy_hash="policy-old",
        t0=EPOCH,
    )
    new_policy = EpochPolicy(
        required_sources=old_policy.required_sources,
        symbols=old_policy.symbols,
        study_id="study-new",
        policy_hash="policy-new",
        t0=EPOCH + dt.timedelta(days=23),
    )
    with AppendOnlyPITStore(tmp_path) as store:
        _append(store, "binance_usdm.basis")
        EpochLedger(store._db, old_policy).append_process_version(  # noqa: SLF001
            collector_instance_id="replica-a",
            run_id="old-run",
            started_at=EPOCH - dt.timedelta(hours=5),
            code_hash="a" * 40,
            collector_version=collector_module.COLLECTOR_VERSION,
        )
        new_ledger = EpochLedger(store._db, new_policy)  # noqa: SLF001

        with pytest.raises(T0StartupGateError, match="G-T0-B") as error:
            new_ledger.validate_t0_startup(
                started_at=new_policy.t0 + dt.timedelta(hours=6)
            )

        assert "현재 study_id/policy_hash stamp 없음" in str(error.value)
        assert "새 커밋·새 T0" in str(error.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_t0_startup_gate_runs_before_collection_or_network(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network_calls = 0

    async def forbidden_network(*_args, **_kwargs) -> None:
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("collection task started before T0 gate")

    monkeypatch.setattr(
        collector_module,
        "utc_now",
        lambda: dt.datetime(2026, 7, 30, tzinfo=dt.UTC),
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
        monkeypatch.setattr(collector, "_ws_supervisor", forbidden_network)
        monkeypatch.setattr(collector, "_poll_loop", forbidden_network)

        with pytest.raises(T0StartupGateError, match="G-T0-B"):
            await collector.run()

        assert network_calls == 0
        assert (
            store._db.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM collector_process_versions"
            ).fetchone()[0]
            == 0
        )
        assert (
            store._db.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM collector_attempt_starts"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.unit
def test_process_version_t0_additive_migration_preserves_legacy_rows(tmp_path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE collector_process_versions (
            append_id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_stamp_id TEXT NOT NULL UNIQUE,
            study_id TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            collector_instance_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            collector_version TEXT NOT NULL,
            UNIQUE (study_id, policy_hash, collector_instance_id, run_id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO collector_process_versions (
            version_stamp_id, study_id, policy_hash, collector_instance_id,
            run_id, started_at, code_hash, collector_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-stamp",
            POLICY.study_id,
            POLICY.policy_hash,
            "replica-a",
            "legacy-run",
            "2026-07-26T00:00:00.000000Z",
            "a" * 40,
            collector_module.COLLECTOR_VERSION,
        ),
    )
    connection.commit()

    try:
        EpochLedger(connection, POLICY)
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(collector_process_versions)"
            )
        }
        row = connection.execute(
            "SELECT version_stamp_id, t0_utc FROM collector_process_versions"
        ).fetchone()
        assert "t0_utc" in columns
        assert dict(row) == {"version_stamp_id": "legacy-stamp", "t0_utc": None}
    finally:
        connection.close()


@pytest.mark.unit
def test_watchdog_availability_detects_deployed_head_mismatch(tmp_path) -> None:
    expected_code_hash = "a" * 40
    stores: list[AppendOnlyPITStore] = []
    try:
        for name, code_hash in (("a", expected_code_hash), ("b", "b" * 40)):
            store = AppendOnlyPITStore(tmp_path / name)
            stores.append(store)
            ledger = EpochLedger(store._db, POLICY)  # noqa: SLF001
            ledger.append_process_version(
                collector_instance_id=f"replica-{name}",
                run_id=f"run-{name}",
                started_at=RECEIVED,
                code_hash=code_hash,
                collector_version=collector_module.COLLECTOR_VERSION,
            )
            ledger.append_heartbeat(
                collector_instance_id=f"replica-{name}",
                run_id=f"run-{name}",
                observed_at=RECEIVED,
                health={"ok": True},
            )

        report = availability_report(
            [store.path for store in stores],
            POLICY,
            observed_at=RECEIVED + dt.timedelta(seconds=30),
            stale_after_seconds=60,
            expected_code_hash=expected_code_hash,
        )

        assert report["healthy_replica_count"] == 1
        assert report["version_stamp_match"] is False
        assert report["unstamped_artifacts"] == []
        assert report["version_mismatches"] == [
            {
                "actual_code_hash": "b" * 40,
                "artifact": str(stores[1].path),
                "expected_code_hash": expected_code_hash,
            }
        ]
        assert [replica["status"] for replica in report["replicas"]] == [
            "HEALTHY",
            "VERSION_MISMATCH",
        ]
    finally:
        for store in stores:
            store.close()


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
