from __future__ import annotations

import datetime as dt
import json
import sqlite3

import httpx
import pytest

from app.services.brokers.binance import r4_p0_collector as collector_module
from app.services.brokers.binance.r4_p0_collector import (
    BASIS_RECOVERY_LIMIT,
    PIT_COLUMNS,
    AppendOnlyPITStore,
    BasisPollError,
    BinanceR4P0Collector,
    CollectorConfig,
    assert_rest_target,
    assert_ws_target,
    build_ws_urls,
)


@pytest.mark.unit
def test_allowlists_are_read_only_and_exact() -> None:
    for path in (
        "/fapi/v1/openInterest",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/premiumIndexKlines",
        "/futures/data/basis",
        "/futures/data/openInterestHist",
        "/futures/data/takerlongshortRatio",
    ):
        assert_rest_target(path)
    with pytest.raises(ValueError):
        assert_rest_target("/fapi/v1/order")
    with pytest.raises(ValueError):
        assert_rest_target("/fapi/v2/account")
    with pytest.raises(ValueError):
        assert_ws_target("wss://demo-fapi.binance.com/ws")
    urls = build_ws_urls()
    assert set(urls) == {"public", "market"}
    assert "/public/stream?" in urls["public"]
    assert "aggTrade" not in urls["public"]
    assert "/market/stream?" in urls["market"]
    assert "aggTrade" in urls["market"]
    assert "forceOrder" in urls["market"]


@pytest.mark.unit
def test_store_is_append_only_deduplicated_and_auditable(tmp_path) -> None:
    now = dt.datetime(2026, 7, 26, 1, 2, 3, tzinfo=dt.UTC)
    kwargs = {
        "source": "binance_usdm.aggTrade",
        "symbol": "XRPUSDT",
        "raw_payload": {"a": 123, "E": 1785027723000},
        "local_receive_time": now,
        "run_id": "run-1",
        "event_time": "2026-07-26T01:02:03.000000Z",
        "transaction_time": "2026-07-26T01:02:03.000000Z",
        "request_started_at": "2026-07-26T01:02:02.000000Z",
        "request_completed_at": "2026-07-26T01:02:02.500000Z",
        "sequence_or_trade_id": "123",
        "gap_detected": False,
        "reconnect_id": "run-1:ws:1",
    }
    with AppendOnlyPITStore(tmp_path) as store:
        assert store.append(**kwargs)
        assert not store.append(**{**kwargs, "run_id": "run-2"})
        result = store.audit()
        assert result["ok"]
        assert result["rows"] == 1
        row = store.sample_by_source()[0]
        assert set(PIT_COLUMNS).issubset(row)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._db.execute("UPDATE pit_records SET symbol = 'DOGEUSDT'")  # noqa: SLF001
        store._db.rollback()  # noqa: SLF001
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._db.execute("DELETE FROM pit_records")  # noqa: SLF001

    # Reopening proves restart-safe deduplication against persisted identity.
    with AppendOnlyPITStore(tmp_path) as reopened:
        assert not reopened.append(**{**kwargs, "run_id": "run-3"})
        assert reopened.audit()["rows"] == 1


@pytest.mark.unit
def test_websocket_payloads_keep_exchange_and_receive_times(tmp_path) -> None:
    config = CollectorConfig(artifact_root=tmp_path)
    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(config, store)
        collector._handle_ws_raw(  # noqa: SLF001
            json.dumps(
                {
                    "stream": "xrpusdt@aggTrade",
                    "data": {
                        "e": "aggTrade",
                        "E": 1785027723001,
                        "s": "XRPUSDT",
                        "a": 42,
                        "p": "1.23",
                        "q": "2",
                        "T": 1785027723000,
                        "m": True,
                    },
                }
            ),
            received=dt.datetime(2026, 7, 26, 1, 2, 4, tzinfo=dt.UTC),
            request_started=dt.datetime(2026, 7, 26, 1, 2, 0, tzinfo=dt.UTC),
            request_completed=dt.datetime(2026, 7, 26, 1, 2, 1, tzinfo=dt.UTC),
            reconnect_id="run:ws:1",
        )
        row = store.sample_by_source()[0]
        assert row["event_time"] == "2026-07-26T01:02:03.001000Z"
        assert row["transaction_time"] == "2026-07-26T01:02:03.000000Z"
        assert row["local_receive_time"] == "2026-07-26T01:02:04.000000Z"
        assert json.loads(row["raw_payload"])["a"] == 42


@pytest.mark.unit
def test_restart_marks_first_new_connection_record_as_gap(tmp_path) -> None:
    config = CollectorConfig(artifact_root=tmp_path)
    received = dt.datetime(2026, 7, 26, 1, 2, 4, tzinfo=dt.UTC)
    with AppendOnlyPITStore(tmp_path) as store:
        first = BinanceR4P0Collector(config, store)
        first._handle_ws_raw(  # noqa: SLF001
            json.dumps(
                {
                    "stream": "xrpusdt@bookTicker",
                    "data": {
                        "e": "bookTicker",
                        "E": 1785027723001,
                        "T": 1785027723000,
                        "s": "XRPUSDT",
                        "u": 9,
                    },
                }
            ),
            received=received,
            request_started=received,
            request_completed=received,
            reconnect_id="run-1:ws:1",
        )
    with AppendOnlyPITStore(tmp_path) as store:
        second = BinanceR4P0Collector(config, store)
        second._handle_ws_raw(  # noqa: SLF001
            json.dumps(
                {
                    "stream": "xrpusdt@bookTicker",
                    "data": {
                        "e": "bookTicker",
                        "E": 1785027724001,
                        "T": 1785027724000,
                        "s": "XRPUSDT",
                        "u": 10,
                    },
                }
            ),
            received=received + dt.timedelta(seconds=1),
            request_started=received,
            request_completed=received,
            reconnect_id="run-2:ws:1",
        )
        rows = list(
            store._db.execute(  # noqa: SLF001
                "SELECT gap_detected FROM pit_records ORDER BY append_id"
            )
        )
        assert [row["gap_detected"] for row in rows] == [0, 1]


@pytest.mark.unit
def test_health_fails_closed_for_zero_row_required_sources(tmp_path) -> None:
    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(CollectorConfig(artifact_root=tmp_path), store)
        health = collector.health()
        assert not health["ok"]
        assert "binance_usdm.aggTrade" in health["missing_required_sources"]
        assert health["missing_sparse_sources"] == ["binance_usdm.forceOrder"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_premium_kline_persists_only_a_completed_period(tmp_path) -> None:
    now_ms = int(dt.datetime.now(tz=dt.UTC).timestamp() * 1000)
    closed = [
        now_ms - 120_000,
        "0.1",
        "0.2",
        "0.0",
        "0.1",
        "0",
        now_ms - 60_000,
    ]
    active = [
        now_ms,
        "0.2",
        "0.3",
        "0.1",
        "0.2",
        "0",
        now_ms + 60_000,
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/premiumIndexKlines"
        return httpx.Response(200, json=[closed, active])

    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(CollectorConfig(artifact_root=tmp_path), store)
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            await collector._rest_premium_kline(  # noqa: SLF001
                client, symbol="XRPUSDT"
            )
        row = store.sample_by_source()[0]
        assert row["source"] == "binance_usdm.premiumIndexKline1m"
        assert json.loads(row["raw_payload"]) == closed
        assert row["event_time"] == row["transaction_time"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_premium_kline_long_poll_recovers_every_completed_slot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = dt.datetime(2026, 7, 29, tzinfo=dt.UTC)
    clock = iter(
        (
            base + dt.timedelta(minutes=1, seconds=10),
            base + dt.timedelta(minutes=1, seconds=11),
            base + dt.timedelta(minutes=4, seconds=10),
            base + dt.timedelta(minutes=4, seconds=11),
        )
    )
    monkeypatch.setattr(collector_module, "utc_now", lambda: next(clock))
    calls = 0

    def kline(minute: int) -> list[object]:
        opened = base + dt.timedelta(minutes=minute)
        closed = opened + dt.timedelta(minutes=1) - dt.timedelta(milliseconds=1)
        return [
            int(opened.timestamp() * 1000),
            "0.1",
            "0.2",
            "0.0",
            "0.1",
            "0",
            int(closed.timestamp() * 1000),
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert dict(request.url.params) == {
                "symbol": "XRPUSDT",
                "interval": "1m",
                "limit": "2",
            }
            return httpx.Response(200, json=[kline(0), kline(1)])
        if "startTime" not in request.url.params:
            # Current HEAD's latest-two request permanently skips minutes 1 and 2.
            return httpx.Response(200, json=[kline(3), kline(4)])
        assert request.url.params["startTime"] == str(kline(0)[6] + 1)
        return httpx.Response(
            200,
            json=[kline(1), kline(2), kline(3), kline(4)],
        )

    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(artifact_root=tmp_path, symbols=("XRPUSDT",)),
            store,
        )
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            await collector._rest_premium_kline(  # noqa: SLF001
                client, symbol="XRPUSDT"
            )
            await collector._rest_premium_kline(  # noqa: SLF001
                client, symbol="XRPUSDT"
            )

        rows = list(
            store._db.execute(  # noqa: SLF001
                """
                SELECT event_time
                FROM pit_records
                WHERE source = 'binance_usdm.premiumIndexKline1m'
                ORDER BY event_time
                """
            )
        )
        assert [row["event_time"] for row in rows] == [
            "2026-07-29T00:00:59.999000Z",
            "2026-07-29T00:01:59.999000Z",
            "2026-07-29T00:02:59.999000Z",
            "2026-07-29T00:03:59.999000Z",
        ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_interest_family_keeps_current_and_bounded_history(tmp_path) -> None:
    timestamp = 1785027600000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/openInterest":
            assert request.url.params["symbol"] == "XRPUSDT"
            return httpx.Response(
                200,
                json={
                    "symbol": "XRPUSDT",
                    "openInterest": "335606096.1",
                    "time": timestamp,
                },
            )
        assert request.url.path == "/futures/data/openInterestHist"
        assert dict(request.url.params) == {
            "symbol": "XRPUSDT",
            "period": "5m",
            "limit": "1",
        }
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "XRPUSDT",
                    "sumOpenInterest": "335602101.0",
                    "sumOpenInterestValue": "368798010.1",
                    "CMCCirculatingSupply": "59985132502",
                    "timestamp": timestamp,
                }
            ],
        )

    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(artifact_root=tmp_path, symbols=("XRPUSDT",)),
            store,
        )
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            await collector._poll_family(client, "open_interest")  # noqa: SLF001
        samples = {row["source"]: row for row in store.sample_by_source()}
        assert set(samples) == {
            "binance_usdm.openInterest",
            "binance_usdm.openInterestHist",
        }
        assert samples["binance_usdm.openInterestHist"]["event_time"] == (
            "2026-07-26T01:00:00.000000Z"
        )
        assert set(PIT_COLUMNS).issubset(samples["binance_usdm.openInterestHist"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basis_empty_response_is_missing_without_starving_other_symbols(
    tmp_path,
) -> None:
    timestamp = 1785027600000
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["pair"]
        requested.append(symbol)
        assert request.url.params["limit"] == str(BASIS_RECOVERY_LIMIT)
        if symbol == "DOGEUSDT":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {
                    "pair": symbol,
                    "contractType": "PERPETUAL",
                    "timestamp": timestamp,
                    "basis": "0.1",
                }
            ],
        )

    symbols = ("XRPUSDT", "DOGEUSDT", "SOLUSDT", "BTCUSDT")
    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(artifact_root=tmp_path, symbols=symbols),
            store,
        )
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(
                BasisPollError,
                match=r"DOGEUSDT=MissingBasisDataError",
            ):
                await collector._poll_family(client, "basis")  # noqa: SLF001

        assert requested == list(symbols)
        rows = list(
            store._db.execute(  # noqa: SLF001
                """
                SELECT symbol, event_time
                FROM pit_records
                WHERE source = 'binance_usdm.basis'
                ORDER BY symbol
                """
            )
        )
        assert [row["symbol"] for row in rows] == [
            "BTCUSDT",
            "SOLUSDT",
            "XRPUSDT",
        ]
        assert {row["event_time"] for row in rows} == {"2026-07-26T01:00:00.000000Z"}
        attempts = list(
            store._db.execute(  # noqa: SLF001
                """
                SELECT request_identity_json, terminal_status, error_type,
                       error_message, error_traceback, response_body_summary
                FROM collector_attempts ORDER BY append_id
                """
            )
        )
        assert len(attempts) == len(symbols)
        failed = [row for row in attempts if row["terminal_status"] != "SUCCESS"]
        assert len(failed) == 1
        assert failed[0]["terminal_status"] == "INVALID_RESPONSE"
        assert failed[0]["error_type"] == "MissingBasisDataError"
        assert "DOGEUSDT" in failed[0]["error_message"]
        assert "MissingBasisDataError" in failed[0]["error_traceback"]
        assert failed[0]["response_body_summary"] == "[]"
        identities = [json.loads(row["request_identity_json"]) for row in attempts]
        assert all(
            identity["sources"] == ["binance_usdm.basis"] for identity in identities
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basis_replays_recent_rows_to_repair_transient_gap(tmp_path) -> None:
    initial_timestamp = 1785027000000
    recovery_timestamps = (
        1785026700000,
        initial_timestamp,
        1785027300000,
        1785027600000,
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        timestamps = (initial_timestamp,) if calls == 1 else recovery_timestamps
        return httpx.Response(
            200,
            json=[
                {
                    "pair": "XRPUSDT",
                    "contractType": "PERPETUAL",
                    "timestamp": timestamp,
                    "basis": str(timestamp),
                }
                for timestamp in timestamps
            ],
        )

    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(artifact_root=tmp_path, symbols=("XRPUSDT",)),
            store,
        )
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            await collector._poll_family(client, "basis")  # noqa: SLF001
            await collector._poll_family(client, "basis")  # noqa: SLF001

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
        assert [row["event_time"] for row in rows] == [
            "2026-07-26T00:50:00.000000Z",
            "2026-07-26T00:55:00.000000Z",
            "2026-07-26T01:00:00.000000Z",
        ]
        assert collector.session_counts["binance_usdm.basis"] == 3
        assert collector.duplicate_counts["binance_usdm.basis"] == 1
        attempts = list(
            store._db.execute(  # noqa: SLF001
                """
                SELECT terminal_status, request_identity_json
                FROM collector_attempts ORDER BY append_id
                """
            )
        )
        assert [row["terminal_status"] for row in attempts] == [
            "SUCCESS",
            "SUCCESS",
        ]
        assert all(
            json.loads(row["request_identity_json"])["sources"]
            == ["binance_usdm.basis"]
            for row in attempts
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basis_conflicting_replay_preserves_both_payloads(tmp_path) -> None:
    timestamp = 1785027600000
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=[
                {
                    "pair": "XRPUSDT",
                    "contractType": "PERPETUAL",
                    "timestamp": timestamp,
                    "basis": str(calls),
                }
            ],
        )

    with AppendOnlyPITStore(tmp_path) as store:
        collector = BinanceR4P0Collector(
            CollectorConfig(artifact_root=tmp_path, symbols=("XRPUSDT",)),
            store,
        )
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            await collector._poll_family(client, "basis")  # noqa: SLF001
            await collector._poll_family(client, "basis")  # noqa: SLF001

        rows = list(
            store._db.execute(  # noqa: SLF001
                """
                SELECT event_time, raw_payload_sha256
                FROM pit_records
                WHERE source = 'binance_usdm.basis'
                ORDER BY append_id
                """
            )
        )
        assert len(rows) == 2
        assert rows[0]["event_time"] == rows[1]["event_time"]
        assert rows[0]["raw_payload_sha256"] != rows[1]["raw_payload_sha256"]
