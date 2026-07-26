from __future__ import annotations

import datetime as dt
import json
import sqlite3

import httpx
import pytest

from app.services.binance_r4_p0_collector import (
    PIT_COLUMNS,
    AppendOnlyPITStore,
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
