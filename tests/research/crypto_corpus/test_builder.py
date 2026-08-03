"""Network-zero tests for the public-only crypto corpus builder."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import research.crypto_corpus.builder as builder_module
from research.crypto_corpus.artifacts import ArtifactStore
from research.crypto_corpus.builder import (
    Bar,
    CorpusBuilder,
    FetchResult,
    SourceDataError,
    _normalize_binance,
    calculate_request_budget,
)
from research.crypto_corpus.constants import MAX_REQUESTS
from research.crypto_corpus.public_api import ApiResponse


class FakePublicClient:
    """One exact response per requested public URL; no network implementation."""

    def __init__(self, responses: dict[str, ApiResponse]) -> None:
        self.responses = responses
        self.requests_actual = 0

    def get_json(self, venue: str, url: str) -> ApiResponse:
        del venue
        self.requests_actual += 1
        return self.responses[url]


def _response(url: str, venue: str, payload: object) -> ApiResponse:
    return ApiResponse(
        url=url,
        venue=venue,
        status=200,
        body=json.dumps(payload).encode(),
        payload=payload,
        error=None,
        rate_limited=False,
    )


def test_preflight_snapshots_universe_before_request_budget_gate(tmp_path, monkeypatch):
    upbit_url = builder_module.UPBIT_MARKETS_URL
    binance_url = builder_module.BINANCE_EXCHANGE_INFO_URL
    client = FakePublicClient(
        {
            upbit_url: _response(
                upbit_url,
                "upbit_krw",
                [
                    {"market": "KRW-BTC"},
                    {"market": "BTC-ETH"},
                    {"market": "KRW-ETH"},
                ],
            ),
            binance_url: _response(
                binance_url,
                "binance_usdt_spot",
                {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "quoteAsset": "USDT",
                            "isSpotTradingAllowed": True,
                            "status": "TRADING",
                        },
                        {
                            "symbol": "HALTEDUSDT",
                            "quoteAsset": "USDT",
                            "permissions": ["SPOT"],
                            "status": "BREAK",
                        },
                        {
                            "symbol": "BTCFDUSD",
                            "quoteAsset": "FDUSD",
                            "isSpotTradingAllowed": True,
                            "status": "TRADING",
                        },
                    ]
                },
            ),
        }
    )
    monkeypatch.setattr(builder_module, "PROGRESS_LOG", str(tmp_path / "progress.md"))
    builder = CorpusBuilder(
        store=ArtifactStore(tmp_path / "artifacts"),
        client=client,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 8, 3, tzinfo=UTC),
    )

    preflight = builder.preflight()

    assert preflight["status"] == "READY_FOR_COLLECTION"
    assert preflight["universe"]["upbit_krw"] == ["KRW-BTC", "KRW-ETH"]
    # BREAK is intentionally retained: it is part of the frozen spot universe
    # and must become an explicit source gap rather than a silent omission.
    assert preflight["universe"]["binance_usdt_spot"] == ["BTCUSDT", "HALTEDUSDT"]
    assert client.requests_actual == 2
    assert len(preflight["inputs"]) == 2
    for input_record in preflight["inputs"]:
        assert input_record["relative_path"].startswith("inputs/")
        assert (tmp_path / "artifacts" / input_record["relative_path"]).exists()


def test_request_budget_is_conservative_and_includes_snapshots_and_probes():
    plan = calculate_request_budget(upbit_symbols=2, binance_symbols=3)

    assert plan.universe_snapshot_requests == 2
    assert plan.delisted_probe_requests == 2
    assert plan.projected_total == (
        4
        + 2 * (plan.upbit_daily_pages_per_symbol + plan.upbit_hourly_pages_per_symbol)
        + 3
        * (plan.binance_daily_pages_per_symbol + plan.binance_hourly_pages_per_symbol)
    )
    assert plan.projected_total < MAX_REQUESTS


def test_holdout_receipt_is_write_only_after_atomic_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(builder_module, "PROGRESS_LOG", str(tmp_path / "progress.md"))
    store = ArtifactStore(tmp_path / "artifacts")
    builder = CorpusBuilder(
        store=store,
        client=FakePublicClient({}),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 8, 3, tzinfo=UTC),
    )
    builder._preflight_payload = {"inputs": [], "started_at": "2026-08-03T00:00:00Z"}
    bar = Bar(
        venue="upbit_krw",
        symbol="KRW-BTC",
        frequency="1h",
        open_ms=int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000),
        close_exclusive_ms=int(datetime(2025, 1, 1, 1, tzinfo=UTC).timestamp() * 1000),
        open_price=100.0,
        high_price=110.0,
        low_price=90.0,
        close_price=105.0,
        base_volume=1.0,
        quote_volume=105.0,
        trade_count=None,
        source_candle_date_time_utc="2025-01-01T00:00:00",
        source_candle_date_time_kst="2025-01-01T09:00:00",
        source_open_time_ms=None,
        source_close_time_ms=None,
        source_timestamp_ms=1_735_689_600_000,
    )

    builder._persist_task(
        venue="upbit_krw",
        symbol="KRW-BTC",
        frequency="1h",
        result=FetchResult([bar]),
    )

    receipt = store.load_json_records(store.receipts)[0]
    record = receipt["files"][0]
    assert record["is_holdout"] is True
    assert record["relative_path"].startswith("holdout/")
    with pytest.raises(PermissionError):
        store.read_file_bytes(record["relative_path"])


def test_binance_unfinished_bar_is_rejected_before_storage():
    malformed = [
        1_735_689_600_000,
        "100",
        "110",
        "90",
        "105",
        "1",
        1_735_689_600_000 + 3_600_000 - 2,
        "105",
        1,
        "0.5",
        "52.5",
    ]

    with pytest.raises(SourceDataError, match="incomplete/corrupt"):
        _normalize_binance(malformed, "1h", "BTCUSDT")


def test_research_package_has_no_app_db_or_environment_credential_imports():
    root = Path(builder_module.__file__).resolve().parents[0]
    forbidden_roots = {
        "app",
        "sqlalchemy",
        "asyncpg",
        "psycopg",
        "taskiq",
        "prefect",
    }
    for source in root.glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    name.name.split(".")[0] not in forbidden_roots
                    for name in node.names
                )
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden_roots
        assert "os.environ" not in source.read_text()
