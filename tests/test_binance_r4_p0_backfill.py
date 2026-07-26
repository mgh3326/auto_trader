from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal

import httpx
import pytest

from app.services.brokers.binance.r4_p0_backfill import (
    BACKFILL_DB_FILENAME,
    KLINE_SOURCE,
    OI_SOURCE,
    PREMIUM_SOURCE,
    BackfillConfig,
    BackfillPITStore,
    BinanceR4P0Backfill,
    OIObservation,
    assert_backfill_rest_target,
    build_coverage_report,
    floor_utc_4h_ms,
    select_oi_boundary_observation,
)
from app.services.brokers.binance.r4_p0_collector import PIT_COLUMNS, utc_now

FOUR_H_MS = 4 * 60 * 60 * 1000
FIVE_M_MS = 5 * 60 * 1000
NOW = dt.datetime(2026, 7, 26, 12, tzinfo=dt.UTC)


def _kline(open_ms: int, *, total: str = "100", buy: str = "60") -> list[object]:
    return [
        open_ms,
        "1",
        "2",
        "0.5",
        "1.5",
        total,
        open_ms + FOUR_H_MS - 1,
        "100",
        10,
        buy,
        "60",
        "0",
    ]


def _append(
    store: BackfillPITStore,
    *,
    source: str,
    symbol: str,
    payload: object,
    event_ms: int,
) -> None:
    assert store.append(
        source=source,
        symbol=symbol,
        raw_payload=payload,
        local_receive_time=NOW,
        run_id="backfill:test",
        event_time=dt.datetime.fromtimestamp(event_ms / 1000, tz=dt.UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        transaction_time=None,
        request_started_at="2026-07-26T11:59:59.000000Z",
        request_completed_at="2026-07-26T12:00:00.000000Z",
        sequence_or_trade_id=str(event_ms),
        gap_detected=False,
        reconnect_id="backfill:test:rest",
    )


@pytest.mark.unit
def test_backfill_allowlist_is_production_public_get_only() -> None:
    for path in (
        "/fapi/v1/klines",
        "/fapi/v1/premiumIndexKlines",
        "/futures/data/openInterestHist",
    ):
        assert_backfill_rest_target(path)
    for path in ("/fapi/v1/order", "/fapi/v2/account", "/api/v3/account"):
        with pytest.raises(ValueError):
            assert_backfill_rest_target(path)


@pytest.mark.unit
def test_backfill_store_is_separate_and_provenance_sealed(tmp_path) -> None:
    with BackfillPITStore(tmp_path) as store:
        assert store.path.name == BACKFILL_DB_FILENAME
        assert store.provenance()["artifact_kind"] == "historical_rest_backfill"
        assert (
            "not_historical_live_receive_time"
            in store.provenance()["local_receive_time_semantics"]
        )
        columns = {
            row["name"] for row in store._db.execute("PRAGMA table_info(pit_records)")
        }
        assert set(PIT_COLUMNS).issubset(columns)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute(
                "UPDATE artifact_metadata SET value = 'live' "
                "WHERE key = 'artifact_kind'"
            )


@pytest.mark.unit
def test_oi_boundary_prefers_nearest_live_and_flags_gt_one_percent() -> None:
    boundary = 1_000_000
    live = [
        OIObservation(boundary + FIVE_M_MS, Decimal("101"), "live"),
        OIObservation(boundary + 1_000, Decimal("100"), "live"),
    ]
    backfill = [
        OIObservation(boundary, Decimal("98"), "backfill"),
    ]
    selected = select_oi_boundary_observation(
        boundary_ms=boundary, live=live, backfill=backfill
    )
    assert selected.observation == live[1]
    assert selected.integrity_flag
    assert selected.live_backfill_relative_difference == Decimal("0.02")


@pytest.mark.unit
def test_oi_boundary_is_inclusive_at_five_minutes() -> None:
    boundary = 1_000_000
    edge = OIObservation(boundary - FIVE_M_MS, Decimal("10"), "backfill")
    selected = select_oi_boundary_observation(
        boundary_ms=boundary, live=(), backfill=[edge]
    )
    assert selected.observation == edge
    outside = OIObservation(boundary - FIVE_M_MS - 1, Decimal("10"), "backfill")
    assert (
        select_oi_boundary_observation(
            boundary_ms=boundary, live=(), backfill=[outside]
        ).observation
        is None
    )


@pytest.mark.unit
def test_coverage_uses_t25_kline_ofi_and_complete_open_time_join(tmp_path) -> None:
    target_epochs = 2
    cutoff = int(NOW.timestamp() * 1000)
    opens = [cutoff - 2 * FOUR_H_MS, cutoff - FOUR_H_MS]
    with BackfillPITStore(tmp_path) as store:
        for symbol in ("XRPUSDT", "DOGEUSDT", "SOLUSDT", "BTCUSDT"):
            for open_ms in opens:
                _append(
                    store,
                    source=KLINE_SOURCE,
                    symbol=symbol,
                    payload=_kline(open_ms),
                    event_ms=open_ms,
                )
                _append(
                    store,
                    source=PREMIUM_SOURCE,
                    symbol=symbol,
                    payload=_kline(open_ms, total="0", buy="0"),
                    event_ms=open_ms,
                )
            for timestamp in range(opens[0], cutoff + FIVE_M_MS, FIVE_M_MS):
                _append(
                    store,
                    source=OI_SOURCE,
                    symbol=symbol,
                    payload={
                        "symbol": symbol,
                        "sumOpenInterest": "10",
                        "timestamp": timestamp,
                    },
                    event_ms=timestamp,
                )
        report = build_coverage_report(
            store,
            target_epochs=target_epochs,
            observed_at=NOW,
            oi_boundary_proofs={
                symbol: {"confirmed": True}
                for symbol in ("XRPUSDT", "DOGEUSDT", "SOLUSDT", "BTCUSDT")
            },
        )
    assert report["acceptance"]["three_signal_symbols_ofi_premium_252_100pct"]
    for symbol in ("XRPUSDT", "DOGEUSDT", "SOLUSDT"):
        assert report["symbols"][symbol]["ofi"]["covered_target_epochs"] == 2
        assert report["symbols"][symbol]["premium"]["covered_target_epochs"] == 2
        assert (
            report["symbols"][symbol]["open_interest"]["boundary_pair_eligible_epochs"]
            == 2
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_network_runner_filters_active_rows_and_paginates_oi(tmp_path) -> None:
    target_epochs = 2
    cutoff = floor_utc_4h_ms(utc_now())
    opens = [cutoff - 2 * FOUR_H_MS, cutoff - FOUR_H_MS]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path in {
            "/fapi/v1/klines",
            "/fapi/v1/premiumIndexKlines",
        }:
            return httpx.Response(
                200,
                json=[_kline(open_ms) for open_ms in opens],
                headers={"x-mbx-used-weight-1m": "12"},
            )
        assert request.url.path == "/futures/data/openInterestHist"
        assert "startTime" not in request.url.params
        if calls.count(request.url.path) == 1:
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "XRPUSDT",
                        "sumOpenInterest": "10",
                        "timestamp": cutoff,
                    }
                ],
            )
        return httpx.Response(200, json=[])

    config = BackfillConfig(
        artifact_root=tmp_path,
        symbols=("XRPUSDT",),
        target_epochs=target_epochs,
        request_delay_seconds=0,
    )
    with BackfillPITStore(tmp_path) as store:
        runner = BinanceR4P0Backfill(config, store)
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com", transport=transport
        ) as client:
            await runner._fetch_kline_family(
                client,
                symbol="XRPUSDT",
                path="/fapi/v1/klines",
                source=KLINE_SOURCE,
            )
            await runner._fetch_kline_family(
                client,
                symbol="XRPUSDT",
                path="/fapi/v1/premiumIndexKlines",
                source=PREMIUM_SOURCE,
            )
            await runner._fetch_oi_history(client, symbol="XRPUSDT")
        rows = list(store._db.execute("SELECT * FROM pit_records"))
    assert len(rows) == 5
    assert all(row["collector_version"].endswith("backfill.v1") for row in rows)
    assert all(row["run_id"].startswith("backfill:") for row in rows)
    assert json.loads(rows[-1]["raw_payload"])["sumOpenInterest"] == "10"
