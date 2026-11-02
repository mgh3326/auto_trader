"""Offline acceptance tests for broad, atomic paper-runner data snapshots."""

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
import pytest

from app.services.mock_strategy.crypto_signals import daily_lists
from research.stock_selection.dataset import validate_bars
from research.stock_selection.io import read_bars
from scripts import mock_data_refresh as producer
from scripts.mock_data_sources import (
    AlpacaSource,
    BinanceSource,
    JsonGet,
    canonical,
    crypto_assets,
    us_assets,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


class FakeSource:
    feed = "fake"

    def __init__(self, market="crypto", count=24):
        self.market = market
        self.benchmark = "BTCUSDT" if market == "crypto" else "SPY"
        self.names = [f"COIN{i:02}USDT" for i in range(count)]
        self.calls = []
        self.fail = False
        self.revised = False

    def assets(self):
        return list(self.names)

    def bars(self, symbols, start, end):
        self.calls.append((symbols, start, end))
        if self.fail:
            raise ValueError("fake fetch failed")
        rows = []
        for day in producer.sessions(self.market, start.isoformat(), end.isoformat()):
            # Price is a pure function of date, independent of request window.
            t = (day.date() - date(2024, 1, 1)).days
            for symbol in symbols:
                rank = int(symbol[4:6]) + 1 if symbol.startswith("COIN") else 30
                price = 100 + rank + t * 0.01 + (t % (rank + 1)) * 0.1
                if self.revised and symbol == self.names[0]:
                    price *= 0.5
                rows.append(
                    {
                        "date": day.date().isoformat(),
                        "symbol": symbol,
                        "open": price,
                        "high": price + 1,
                        "low": price - 1,
                        "close": price,
                        "volume": rank * 100,
                        "traded_value": rank * 1000,
                        "action": False,
                    }
                )
        return canonical(rows, self.market)


@pytest.mark.parametrize("market", ["us", "crypto"])
def test_roundtrip_rank_and_full_feature_history(tmp_path, market):
    source = FakeSource(market)
    meta = producer.refresh(tmp_path, source, now=NOW, limit=20)
    bars, actual = read_bars(tmp_path, market)
    validate_bars(bars, market)
    assert actual == meta and meta["symbols"] == 20
    assert bars.groupby("symbol").size().min() >= (260 if market == "us" else 365)
    members = json.loads((tmp_path / market / "membership.json").read_text())
    assert set(members) == {"2026-11-02"}
    assert members["2026-11-02"]["symbols"] == list(reversed(source.names[4:]))
    assert source.names[0] not in set(bars.symbol)
    assert "__benchmark__" in set(bars.symbol)
    assert pd.to_datetime(bars.available_at, utc=True).max() <= NOW
    if market == "crypto":
        payload = daily_lists(None, date(2026, 11, 1), cache_root=tmp_path)
        assert len(payload["horizons"]["3"]["up"]) == 10
        assert payload["universe_observed_at"] == NOW.isoformat()
        assert pd.Timestamp(payload["prediction_at"]) >= pd.Timestamp(NOW)
        with pytest.raises(ValueError, match="future universe"):
            daily_lists(None, date(2026, 10, 31), cache_root=tmp_path)


def test_rerun_no_network_no_writes_and_membership_never_backfilled(tmp_path):
    source = FakeSource()
    first = producer.refresh(tmp_path, source, now=NOW, limit=20)
    path = (tmp_path / "crypto").resolve()
    original = {p.name: p.read_bytes() for p in path.iterdir()}
    source.fail = True
    assert (
        producer.refresh(tmp_path, source, now=NOW + timedelta(hours=1), limit=20)
        == first
    )
    assert len(source.calls) == 1
    assert original == {p.name: p.read_bytes() for p in path.iterdir()}
    source.fail = False
    source.names = source.names[1:] + ["COIN99USDT"]
    producer.refresh(tmp_path, source, now=NOW + timedelta(days=1), limit=20)
    members = json.loads((tmp_path / "crypto/membership.json").read_text())
    assert "COIN99USDT" not in members["2026-11-02"]["symbols"]
    assert "COIN99USDT" in members["2026-11-03"]["symbols"]
    assert (
        json.loads(original["membership.json"])["2026-11-02"] == members["2026-11-02"]
    )
    assert source.calls[1][1] > source.calls[0][1]  # daily append, not backfill
    assert any(
        "COIN99USDT" in call[0] and call[1] == source.calls[0][1] + timedelta(days=1)
        for call in source.calls[2:]
    )


@pytest.mark.parametrize("failure", ["fetch", "write", "switch"])
def test_partial_failure_preserves_complete_snapshot(tmp_path, monkeypatch, failure):
    source = FakeSource()
    producer.refresh(tmp_path, source, now=NOW, limit=20)
    before, meta = read_bars(tmp_path, "crypto")
    pointer = (tmp_path / "crypto").resolve()
    if failure == "fetch":
        source.fail = True
    elif failure == "write":
        real_write = producer._write

        def broken(path, data):
            if path.name == "bars.json":
                raise OSError("disk full")
            real_write(path, data)

        monkeypatch.setattr(producer, "_write", broken)
    else:
        monkeypatch.setattr(
            producer.os,
            "replace",
            lambda *a: (_ for _ in ()).throw(OSError("switch failed")),
        )
    with pytest.raises((ValueError, OSError)):
        producer.refresh(tmp_path, source, now=NOW + timedelta(days=1), limit=20)
    after, new_meta = read_bars(tmp_path, "crypto")
    pd.testing.assert_frame_equal(before, after)
    assert meta == new_meta and pointer == (tmp_path / "crypto").resolve()
    assert not list((tmp_path / ".crypto-snapshots").glob(".stage-*"))


def test_adjustment_revision_refetches_history(tmp_path):
    source = FakeSource("us")
    producer.refresh(tmp_path, source, now=NOW, limit=20)
    source.revised = True
    producer.refresh(tmp_path, source, now=NOW + timedelta(days=1), limit=20)
    assert len(source.calls) == 3
    symbols, start, _ = source.calls[-1]
    assert symbols == [source.names[0]]
    assert start < NOW.date() - timedelta(days=260)


def test_offline_dry_run_and_legacy_root_refusal(tmp_path):
    source = FakeSource()
    plan = producer.refresh(tmp_path / "absent", source, now=NOW, dry_run=True)
    assert plan["dry_run"] and not source.calls and not (tmp_path / "absent").exists()
    (tmp_path / "crypto").mkdir()
    with pytest.raises(ValueError, match="fresh root"):
        producer.refresh(tmp_path, source, now=NOW)


def test_asset_filters():
    stock = {
        "symbol": "ABC",
        "name": "ABC Common Stock",
        "status": "active",
        "tradable": True,
        "class": "us_equity",
        "exchange": "NYSE",
    }
    assets = [stock, {**stock, "symbol": "BRK.B"}]
    for i, change in enumerate(
        (
            {"name": "Example ETF"},
            {"name": "Example Warrants"},
            {"name": "Example Units"},
            {"name": "Example Preferred"},
            {"exchange": "OTC"},
            {"tradable": False},
            {"status": "inactive"},
        )
    ):
        assets.append({**stock, "symbol": f"X{chr(65 + i)}", **change})
    assets.append(
        {**stock, "symbol": "DLR", "name": "Digital Realty Trust Common Stock"}
    )
    assert us_assets(assets) == ["ABC", "BRK.B", "DLR"]
    pair = {
        "symbol": "BTCUSDT",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "status": "TRADING",
        "isSpotTradingAllowed": True,
    }
    bad = [
        {**pair, "symbol": s + "USDT", "baseAsset": s}
        for s in ("USDC", "FDUSD", "ETHUP", "ETHDOWN")
    ]
    assert crypto_assets(
        {"symbols": [{**pair, "symbol": "JUPUSDT", "baseAsset": "JUP"}]}
    ) == ["JUPUSDT"]
    assert crypto_assets(
        {"symbols": [pair, *bad, {**pair, "symbol": "ETHUSDT", "status": "BREAK"}]}
    ) == ["BTCUSDT"]


def test_alpaca_pagination_adjustment_feed_and_secret_errors(monkeypatch):
    monkeypatch.setenv("ALPACA_DATA_API_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_DATA_API_SECRET_KEY", "test-secret")
    monkeypatch.setattr("scripts.mock_data_sources.time.sleep", lambda _: None)
    seen = []

    def handler(request):
        seen.append(request)
        bar = {"t": "2026-10-30T04:00:00Z", "o": 10, "h": 12, "l": 9, "c": 11, "v": 100}
        if "page_token" not in request.url.params:
            return httpx.Response(
                200, json={"bars": {"ABC": [bar]}, "next_page_token": "next"}
            )
        return httpx.Response(
            200, json={"bars": {"BRK.B": [bar]}, "next_page_token": None}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = AlpacaSource(JsonGet(client), "sip")
        bars = source.bars(["ABC", "BRK.B"], date(2026, 10, 30), date(2026, 10, 30))
    assert set(bars.symbol) == {"ABC", "BRK.B"}
    assert all(
        r.method == "GET"
        and r.url.params["adjustment"] == "all"
        and r.url.params["feed"] == "sip"
        for r in seen
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(403, text="test-secret"))
    ) as client:
        with pytest.raises(ValueError, match="^market data request failed$") as caught:
            JsonGet(client).get("https://data.alpaca.markets/v2/stocks/bars")
        assert caught.value.__context__ is None


def test_binance_utc_quote_volume(monkeypatch):
    monkeypatch.setattr("scripts.mock_data_sources.time.sleep", lambda _: None)
    stamp = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp() * 1000)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                [
                    stamp,
                    "10",
                    "12",
                    "9",
                    "11",
                    "100",
                    stamp + 86400000 - 1,
                    "1234",
                    10,
                    "0",
                    "0",
                    "0",
                ]
            ],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bars = BinanceSource(JsonGet(client)).bars(
            ["BTCUSDT"], date(2026, 11, 1), date(2026, 11, 1)
        )
    assert bars.iloc[0].traded_value == 1234
    assert bars.iloc[0].available_at.startswith("2026-11-02T00:05:00")
    assert seen[0].url.params["timeZone"] == "0"


def test_successful_empty_candidate_is_reported(tmp_path):
    source = FakeSource()
    original = source.bars

    def omit(symbols, start, end):
        frame = original(symbols, start, end)
        return frame[frame.symbol != source.names[0]]

    source.bars = omit
    meta = producer.refresh(tmp_path, source, now=NOW, limit=20)
    assert meta["excluded"][source.names[0]] == "incomplete_feature_history"


def test_reader_pins_generation_before_opening_metadata(tmp_path, monkeypatch):
    source = FakeSource()
    producer.refresh(tmp_path, source, now=NOW, limit=20)
    before, meta = read_bars(tmp_path, "crypto")
    old = (tmp_path / "crypto").resolve()
    producer.refresh(tmp_path, source, now=NOW + timedelta(days=1), limit=20)
    new = (tmp_path / "crypto").resolve()
    (tmp_path / "crypto").unlink()
    (tmp_path / "crypto").symlink_to(old)
    real_read = producer.Path.read_text
    switched = False

    def switch_after_resolve(path, *args, **kwargs):
        nonlocal switched
        if path == old / "bars.json" and not switched:
            switched = True
            (tmp_path / "crypto").unlink()
            (tmp_path / "crypto").symlink_to(new)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(producer.Path, "read_text", switch_after_resolve)
    actual, actual_meta = read_bars(tmp_path, "crypto")
    assert switched and actual_meta == meta
    pd.testing.assert_frame_equal(actual, before)


def test_crypto_cli_accepts_explicit_cache_root():
    from scripts.mock_strategy_crypto import parser

    args = parser().parse_args(
        [
            "run",
            "--venue",
            "binance_spot_demo",
            "--asof",
            "2026-11-01",
            "--cache-dir",
            "/data/bars",
            "--dry-run",
        ]
    )
    assert str(args.cache_dir) == "/data/bars" and args.panel_root is None
