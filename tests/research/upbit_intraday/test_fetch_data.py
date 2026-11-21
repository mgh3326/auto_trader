from __future__ import annotations

import ast
import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from research.upbit_intraday import data as D
from research.upbit_intraday import fetch as F
from research.upbit_intraday import rules as R
from research.upbit_intraday.public_http import (
    DisallowedUrlError,
    FetchResponse,
    check_url,
)

START = datetime(2026, 9, 7, 15, 0, tzinfo=UTC)
END = START + timedelta(minutes=15 * 500)


def _candle(market: str, when: datetime, price: int = 100) -> dict:
    return {
        "market": market,
        "candle_date_time_utc": when.strftime("%Y-%m-%dT%H:%M:%S"),
        "candle_date_time_kst": "x",
        "opening_price": f"{price}.00000000",
        "high_price": f"{price + 1}.00000000",
        "low_price": f"{price - 1}.00000000",
        "trade_price": f"{price}.50000000",
        "timestamp": 1,
        "candle_acc_trade_price": "1234567.89000000",
        "candle_acc_trade_volume": "12.5",
        "unit": 15,
    }


def _body(candles: list[dict]) -> bytes:
    # Upbit sends numbers unquoted; emulate by dumping then unquoting numerics.
    text = json.dumps(candles)
    for key in (
        "opening_price",
        "high_price",
        "low_price",
        "trade_price",
        "candle_acc_trade_price",
        "candle_acc_trade_volume",
    ):
        for c in candles:
            text = text.replace(f'"{key}": "{c[key]}"', f'"{key}": {c[key]}')
    return text.encode()


class FakeUpbit:
    """Serves candles for slots whose index is not in ``missing`` (no-trade slots)."""

    def __init__(
        self, missing: set[int] = frozenset(), market: str = "KRW-AAA"
    ) -> None:
        self.missing, self.market, self.urls = set(missing), market, []

    def __call__(self, url: str) -> FetchResponse:
        check_url(url)
        self.urls.append(url)
        if "market/all" in url:
            return FetchResponse(
                200,
                json.dumps([{"market": self.market}, {"market": "BTC-ETH"}]).encode(),
            )
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(url).query)
        to = datetime.strptime(q["to"][0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        count = int(q["count"][0])
        out = []
        idx = int((to - START).total_seconds() // 900) - 1
        while idx >= -50 and len(out) < count:
            if idx not in self.missing:
                out.append(
                    _candle(
                        self.market, START + timedelta(minutes=15 * idx), 100 + idx % 7
                    )
                )
            idx -= 1
        return FetchResponse(200, _body(out))


def test_allowlist_blocks_everything_but_the_two_public_reads():
    check_url("https://api.upbit.com/v1/candles/minutes/15?market=KRW-BTC&count=2")
    check_url("https://api.upbit.com/v1/market/all?isDetails=true")
    for bad in (
        "https://api.upbit.com/v1/orders",
        "https://api.upbit.com/v1/accounts",
        "https://api.upbit.com/v1/candles/minutes/1",
        "https://api.upbit.com/v1/candles/minutes/15/../../accounts",
        "http://api.upbit.com/v1/candles/minutes/15",
        "https://example.com/v1/candles/minutes/15",
    ):
        with pytest.raises(DisallowedUrlError):
            check_url(bad)


def test_fetch_market_paginates_and_keeps_gaps(tmp_path: Path):
    missing = {3, 4, 5, 250, 251}
    fake = FakeUpbit(missing)
    rows, hashes = F.fetch_market(
        fake, "KRW-AAA", START, END, F.Pacer(0, sleep=lambda _s: None)
    )
    times = [r.time_utc for r in rows]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert len(rows) == 500 - len(missing)
    assert len(hashes) == len(fake.urls) >= 3
    assert rows[0].time_utc == "2026-09-07T15:00:00"
    # exact decimal text survives
    assert rows[0].quote_volume == "1234567.89000000"
    # gaps stay gaps: no row for a no-trade slot
    assert "2026-09-07T15:45:00" not in times


def test_collect_writes_hashed_files_and_resumes(tmp_path: Path):
    fake = FakeUpbit()
    pacer = F.Pacer(0, sleep=lambda _s: None)
    m1 = F.collect(fake, tmp_path, START, END, workers=1, pacer=pacer)
    meta = m1["markets"]["KRW-AAA"]
    blob = (tmp_path / meta["file"]).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == meta["sha256"] and meta["rows"] == 500
    calls = len(fake.urls)
    F.collect(fake, tmp_path, START, END, workers=1, pacer=pacer)
    assert len(fake.urls) == calls  # resumed: nothing re-fetched
    assert (tmp_path / "manifest.json").read_bytes() == F.canonical_json(m1)
    with gzip.open(tmp_path / meta["file"], "rt") as fh:
        assert fh.readline().strip() == ",".join(F.CSV_HEADER)


def test_collect_records_failures(tmp_path: Path):
    class Failing(FakeUpbit):
        def __call__(self, url: str) -> FetchResponse:
            if "candles" in url:
                return FetchResponse(404, b'{"error":"Code not found"}')
            return super().__call__(url)

    m = F.collect(
        Failing(),
        tmp_path,
        START,
        END,
        workers=1,
        pacer=F.Pacer(0, sleep=lambda _s: None),
    )
    assert "KRW-AAA" in m["failures"] and m["markets"] == {}


def test_loader_builds_nan_gaps_and_rejects_tampering(tmp_path: Path, monkeypatch):
    fake = FakeUpbit({3, 4})
    F.collect(
        fake, tmp_path, START, END, workers=1, pacer=F.Pacer(0, sleep=lambda _s: None)
    )
    s, e = int(START.timestamp()), int(END.timestamp())
    panel, manifest = D.load_panel(tmp_path, start_epoch=s, end_exclusive_epoch=e)
    assert panel.markets == ("KRW-AAA",) and panel.n == 500
    assert (
        np.isnan(panel.o[0, 3])
        and np.isnan(panel.q[0, 4])
        and not np.isnan(panel.o[0, 5])
    )
    assert panel.o[0, 0] == 100.0 and panel.q[0, 0] == pytest.approx(1234567.89)
    with pytest.raises(D.DatasetError, match="frozen window"):
        D.load_panel(tmp_path)  # the real window differs from this tiny fixture
    path = tmp_path / manifest["markets"]["KRW-AAA"]["file"]
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(D.DatasetError, match="hash mismatch"):
        D.load_panel(tmp_path, start_epoch=s, end_exclusive_epoch=e)


def test_warning_markets_reads_the_market_list(tmp_path: Path):
    payload = [
        {"market": "KRW-A", "market_event": {"warning": True, "caution": {"X": False}}},
        {"market": "KRW-B", "market_event": {"warning": False, "caution": {"X": True}}},
        {
            "market": "KRW-C",
            "market_event": {"warning": False, "caution": {"X": False}},
        },
        {"market": "BTC-D", "market_event": {"warning": True, "caution": {}}},
    ]
    (tmp_path / "market_all.json").write_text(json.dumps(payload))
    assert D.warning_markets(tmp_path) == frozenset({"KRW-A", "KRW-B"})


def test_package_has_no_app_broker_or_order_imports():
    root = Path(__file__).resolve().parents[3] / "research" / "upbit_intraday"
    banned = (
        "app",
        "websockets",
        "websocket",
        "sqlalchemy",
        "psycopg",
        "asyncpg",
        "redis",
    )
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                assert name.split(".")[0] not in banned, f"{path.name} imports {name}"
    sources = " ".join(p.read_text() for p in root.glob("*.py"))
    assert "api_key" not in sources.lower() and "authorization" not in sources.lower()


def test_frozen_window_matches_cli_window():
    from research.upbit_intraday import cli

    assert (
        cli.DATA_START == R.DATA_START_UTC
        and cli.DATA_END_EXCLUSIVE == R.DATA_END_EXCLUSIVE_UTC
    )
