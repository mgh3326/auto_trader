"""Archive rebuild end to end on fixtures: delisted pairs land with last trade."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import urllib.parse
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from research.crypto_universe import binance_archive as ba
from research.crypto_universe import build as b
from research.crypto_universe import upbit_public as up
from research.crypto_universe.public_http import (
    DisallowedUrlError,
    FetchResponse,
    check_url,
    with_retries,
)
from research.crypto_universe.universe import PitUniverse

PACKAGE = Path(b.__file__).resolve().parent
SNAP = date(2026, 11, 1)


def _kline_rows(start: date, end: date, *, micro: bool, trades: int = 5) -> str:
    rows = []
    day = start
    while day <= end:
        ms = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
        stamp = ms * 1000 if micro else ms
        rows.append(f"{stamp},1.0,2.0,0.5,1.5,10,{stamp + 1},15,{trades},5,7,0")
        day += timedelta(days=1)
    return "\n".join(rows) + "\n"


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name.replace(".zip", ".csv"), text)
    return buf.getvalue()


class ArchiveFixture:
    """Fake data.binance.vision: S3 listings, zips and checksums."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.symbols: list[str] = []
        self.months: dict[str, list[str]] = {}
        self.requests: list[str] = []

    def add_month(self, symbol: str, month: str, csv_text: str, *, corrupt=False):
        url = ba.month_zip_url(symbol, month)
        name = url.rsplit("/", 1)[1]
        body = _zip(name, csv_text)
        digest = hashlib.sha256(body).hexdigest()
        self.files[url] = body + (b"x" if corrupt else b"")
        self.files[url + ".CHECKSUM"] = f"{digest}  {name}\n".encode()
        if symbol not in self.symbols:
            self.symbols.append(symbol)
        self.months.setdefault(symbol, []).append(month)

    def __call__(self, url: str) -> FetchResponse:
        check_url(url)  # fixtures obey the same allowlist as the real fetcher
        self.requests.append(url)
        if url in self.files:
            return FetchResponse(200, self.files[url])
        parsed = urllib.parse.urlsplit(url)
        if parsed.netloc == "s3-ap-northeast-1.amazonaws.com":
            query = dict(urllib.parse.parse_qsl(parsed.query))
            prefix = query["prefix"]
            if query.get("delimiter") == "/":
                # two pages to exercise pagination
                names = sorted(self.symbols)
                page = [
                    n
                    for n in names
                    if not query.get("marker") or f"{prefix}{n}/" > query["marker"]
                ][:2]
                truncated = len(page) == 2 and page[-1] != names[-1]
                xml = "<ListBucketResult>" + f"<Prefix>{prefix}</Prefix>"
                xml += "".join(
                    f"<CommonPrefixes><Prefix>{prefix}{n}/</Prefix></CommonPrefixes>"
                    for n in page
                )
                if truncated:
                    xml += f"<NextMarker>{prefix}{page[-1]}/</NextMarker>"
                xml += f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"
                return FetchResponse(200, (xml + "</ListBucketResult>").encode())
            symbol = prefix.split("/")[4]
            keys = "".join(
                f"<Key>{prefix}{symbol}-1d-{m}.zip</Key>"
                f"<Key>{prefix}{symbol}-1d-{m}.zip.CHECKSUM</Key>"
                for m in self.months.get(symbol, [])
            )
            xml = f"<ListBucketResult>{keys}<IsTruncated>false</IsTruncated>"
            return FetchResponse(200, (xml + "</ListBucketResult>").encode())
        return FetchResponse(404, b"")


def _exchange_info() -> dict:
    return {
        "symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC",
             "quoteAsset": "USDT"},
            {"symbol": "DEADUSDT", "status": "BREAK", "baseAsset": "DEAD",
             "quoteAsset": "USDT"},
            {"symbol": "NEWUSDT", "status": "TRADING", "baseAsset": "NEW",
             "quoteAsset": "USDT"},
            {"symbol": "PRELISTUSDT", "status": "PRE_TRADING",
             "baseAsset": "PRELIST", "quoteAsset": "USDT"},
        ]
    }  # fmt: skip


@pytest.fixture
def archive() -> ArchiveFixture:
    fx = ArchiveFixture()
    fx.add_month(
        "BTCUSDT",
        "2024-11",
        _kline_rows(date(2024, 11, 1), date(2024, 11, 30), micro=False),
    )
    fx.add_month(
        "BTCUSDT",
        "2024-12",
        _kline_rows(date(2024, 12, 1), date(2024, 12, 31), micro=False),
    )
    # 2025+ spot archives carry microsecond timestamps and a header row
    fx.add_month(
        "BTCUSDT",
        "2025-01",
        "open_time,open,high,low,close,volume,close_time,"
        "quote_volume,count,tbb,tbq,ignore\n"
        + _kline_rows(date(2025, 1, 1), date(2025, 1, 31), micro=True),
    )
    # delisted mid-December 2024: last trade on the 14th, zero-trade bars after
    fx.add_month(
        "DEADUSDT",
        "2024-11",
        _kline_rows(date(2024, 11, 1), date(2024, 11, 30), micro=False),
    )
    fx.add_month(
        "DEADUSDT",
        "2024-12",
        _kline_rows(date(2024, 12, 1), date(2024, 12, 14), micro=False)
        + _kline_rows(date(2024, 12, 15), date(2024, 12, 16), micro=False, trades=0),
    )
    # pair removed from exchangeInfo altogether
    fx.add_month(
        "GONEUSDT",
        "2024-11",
        _kline_rows(date(2024, 11, 3), date(2024, 11, 20), micro=False),
    )
    fx.add_month(
        "NEWUSDT",
        "2025-01",
        _kline_rows(date(2025, 1, 10), date(2025, 1, 31), micro=True),
    )
    return fx  # fmt: skip


def test_rebuild_keeps_delisted_pairs_with_last_trade_date(
    archive: ArchiveFixture, tmp_path: Path
) -> None:
    result = b.build_binance(
        archive,
        tmp_path,
        _exchange_info(),
        snapshot_date=SNAP,
        cache_dir=tmp_path / "cache",
        workers=2,
    )
    by_symbol = {x.symbol: x for x in result.listings}
    assert set(by_symbol) == {"BTCUSDT", "DEADUSDT", "GONEUSDT", "NEWUSDT"}
    assert result.coverage_end == date(2025, 1, 31)

    dead = by_symbol["DEADUSDT"]
    assert (dead.status, dead.exchange_status) == ("delisted", "BREAK")
    assert dead.last_trade_date == date(2024, 12, 14)  # zero-trade bars ignored
    assert dead.last_trade_censored is False
    gone = by_symbol["GONEUSDT"]
    assert (gone.status, gone.exchange_status) == ("delisted", "ABSENT")
    assert (gone.first_trade_date, gone.last_trade_date) == (
        date(2024, 11, 3),
        date(2024, 11, 20),
    )
    btc = by_symbol["BTCUSDT"]
    assert btc.status == "live" and btc.last_trade_date == date(2025, 1, 31)
    assert btc.trading_days == 92 and btc.gap_days == 0  # µs bars parsed as days
    assert result.extra["exchange_info_usdt_pairs_without_archive"] == ["PRELISTUSDT"]

    universe = PitUniverse(result.coverage_start, result.coverage_end, result.listings)
    assert universe.universe_as_of(date(2024, 12, 14)) == {"BTCUSDT", "DEADUSDT"}
    assert universe.universe_as_of(date(2024, 12, 15)) == {"BTCUSDT"}
    assert universe.universe_as_of(date(2025, 1, 10)) == {"BTCUSDT", "NEWUSDT"}

    # candles: split at the crypto-corpus-v1 holdout boundary, labelled
    explo = pq.read_table(
        tmp_path / "candles/binance_usdt_spot/exploration/BTCUSDT.parquet"
    )
    hold = pq.read_table(tmp_path / "candles/binance_usdt_spot/holdout/BTCUSDT.parquet")
    assert explo.num_rows == 61 and hold.num_rows == 31
    meta = explo.schema.metadata
    assert meta[b"crypto_universe.split"] == b"exploration"
    assert meta[b"crypto_universe.survivorship_label"] == b.SURVIVORSHIP_LABEL.encode()
    assert max(explo.column("day").to_pylist()) < b.HOLDOUT_START_DAY

    # coverage report + manifest
    coverage = b.write_outputs(
        tmp_path, {"binance_usdt_spot": result}, {"note": "fixture"},
        now=datetime(2026, 11, 1, tzinfo=UTC),
    )  # fmt: skip
    cov = coverage["venues"]["binance_usdt_spot"]
    assert cov["delisted_pairs"] == 2
    assert cov["delisted_share"] == 0.5
    assert cov["delisted_missing_last_trade_date"] == 0
    assert coverage["current_corpus_delisted_share"] == 0.0
    manifest = json.loads((tmp_path / "universe/manifest.json").read_text())
    table = manifest["tables"]["binance_usdt_spot"]
    text = (tmp_path / table["path"]).read_text()
    assert hashlib.sha256(text.encode()).hexdigest() == table["sha256"]
    assert "DEADUSDT" in text and "2024-12-14" in text
    index = (tmp_path / manifest["candle_files"]["path"]).read_bytes()
    assert hashlib.sha256(index).hexdigest() == manifest["candle_files"]["sha256"]
    for record in json.loads(index)["binance_usdt_spot"]:
        on_disk = (tmp_path / record["path"]).read_bytes()
        assert hashlib.sha256(on_disk).hexdigest() == record["sha256"]
    assert "| binance_usdt_spot |" in (tmp_path / "universe/coverage.md").read_text()


def test_rerun_uses_verified_cache(archive: ArchiveFixture, tmp_path: Path) -> None:
    b.build_binance(
        archive,
        tmp_path,
        _exchange_info(),
        snapshot_date=SNAP,
        cache_dir=tmp_path / "c",
    )
    zips_first = [u for u in archive.requests if u.endswith(".zip")]
    archive.requests.clear()
    b.build_binance(
        archive,
        tmp_path,
        _exchange_info(),
        snapshot_date=SNAP,
        cache_dir=tmp_path / "c",
    )
    assert not [u for u in archive.requests if u.endswith(".zip")]
    assert zips_first


def test_checksum_mismatch_excludes_the_pair(tmp_path: Path) -> None:
    fx = ArchiveFixture()
    fx.add_month(
        "BTCUSDT",
        "2024-11",
        _kline_rows(date(2024, 11, 1), date(2024, 11, 30), micro=False),
    )
    fx.add_month("BADUSDT", "2024-11", _kline_rows(date(2024, 11, 1), date(2024, 11, 30), micro=False),
                 corrupt=True)  # fmt: skip
    result = b.build_binance(
        fx, tmp_path, _exchange_info(), snapshot_date=SNAP, cache_dir=None
    )
    assert [x.symbol for x in result.listings] == ["BTCUSDT"]
    assert result.errors[0]["symbol"] == "BADUSDT"
    assert "checksum mismatch" in result.errors[0]["error"]
    assert not (
        tmp_path / "candles/binance_usdt_spot/exploration/BADUSDT.parquet"
    ).exists()


class UpbitFixture:
    """Markets in ``days_by_market`` serve candles; any other answers 404."""

    def __init__(self, days_by_market: dict[str, list[date]]) -> None:
        self.days_by_market = days_by_market
        self.requests: list[str] = []

    def __call__(self, url: str) -> FetchResponse:
        check_url(url)
        self.requests.append(url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        market = query["market"]
        if market not in self.days_by_market:
            body = b'{"error":{"name":404,"message":"Code not found"}}'
            return FetchResponse(404, body)
        to = date.fromisoformat(query["to"][:10]) if "to" in query else date.max
        count = int(query["count"])
        days = self.days_by_market[market]
        chosen = sorted((d for d in days if d < to), reverse=True)[:count]
        payload = [
            {
                "market": market,
                "candle_date_time_utc": f"{d.isoformat()}T00:00:00",
                "opening_price": 1.0,
                "high_price": 2.0,
                "low_price": 0.5,
                "trade_price": 1.5,
                "candle_acc_trade_volume": 3.0,
                "candle_acc_trade_price": 4.5,
            }
            for d in chosen
        ]
        return FetchResponse(200, json.dumps(payload).encode())


def test_upbit_pages_history_and_records_delisted_probes(tmp_path: Path) -> None:
    days = [date(2023, 1, 1) + timedelta(days=i) for i in range(450)]
    fx = UpbitFixture({"KRW-BTC": days})
    pacer = up.Pacer(interval=0, sleep=lambda _s: None)
    result = b.build_upbit(
        fx, tmp_path, [{"market": "KRW-BTC"}, {"market": "BTC-ETH"}],
        coverage_end=date(2024, 3, 1), delisted_candidates=["KRW-NPXS"], pacer=pacer,
    )  # fmt: skip
    (btc,) = result.listings
    assert btc.first_trade_date == date(2023, 1, 1)
    assert btc.last_trade_date == date(2024, 3, 1)  # clipped at coverage_end
    assert btc.trading_days == (date(2024, 3, 1) - date(2023, 1, 1)).days + 1
    (probe,) = result.extra["delisted_probes"]
    assert probe["market"] == "KRW-NPXS"
    assert probe["http_status"] == 404 and probe["obtainable"] is False
    assert result.extra["delisted_obtained"] == []


@pytest.mark.parametrize(
    "url",
    [
        "https://api.binance.com/api/v3/order",
        "https://api.binance.com/api/v3/klines?symbol=BTCUSDT",
        "https://data-api.binance.vision/api/v3/account",
        "https://api.upbit.com/v1/orders",
        "https://api.upbit.com/v1/accounts",
        "http://data.binance.vision/data/spot/x.zip",
        "https://user:pw@data.binance.vision/data/spot/x.zip",
        "https://data.binance.vision/data/futures/um/x.zip",
    ],
)
def test_fetcher_refuses_anything_but_public_data(url: str) -> None:
    with pytest.raises(DisallowedUrlError):
        check_url(url)


def test_retries_transient_but_returns_404_at_once() -> None:
    calls: list[int] = []
    statuses = iter([503, 429, 200])

    def flaky(url: str) -> FetchResponse:
        calls.append(1)
        return FetchResponse(next(statuses), b"ok")

    assert with_retries(flaky, sleep=lambda _s: None)("u").status == 200
    assert len(calls) == 3

    calls.clear()

    def missing(url: str) -> FetchResponse:
        calls.append(1)
        return FetchResponse(404, b"")

    assert with_retries(missing, sleep=lambda _s: None)("u").status == 404
    assert len(calls) == 1


def test_package_has_no_app_db_broker_or_scheduler_imports() -> None:
    forbidden = ("app", "sqlalchemy", "taskiq", "prefect", "redis", "asyncpg")
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                assert name.split(".")[0] not in forbidden, f"{path.name}: {name}"


def test_upbit_delisted_market_is_ingested_when_a_probe_finds_candles(
    tmp_path: Path,
) -> None:
    dead_days = [date(2019, 1, 1) + timedelta(days=i) for i in range(30)]
    fx = UpbitFixture({"KRW-BTC": [date(2019, 1, 1)], "KRW-DEAD": dead_days})
    pacer = up.Pacer(interval=0, sleep=lambda _s: None)
    result = b.build_upbit(
        fx, tmp_path, [{"market": "KRW-BTC"}],
        coverage_end=date(2024, 3, 1),
        delisted_candidates=["KRW-DEAD", "KRW-NPXS", "KRW-BTC"], pacer=pacer,
    )  # fmt: skip
    by_symbol = {x.symbol: x for x in result.listings}
    assert set(by_symbol) == {"KRW-BTC", "KRW-DEAD"}
    dead = by_symbol["KRW-DEAD"]
    assert (dead.status, dead.exchange_status) == ("delisted", "ABSENT")
    assert dead.last_trade_date == date(2019, 1, 30)
    assert result.extra["delisted_obtained"] == ["KRW-DEAD"]
    probed = [p["market"] for p in result.extra["delisted_probes"]]
    assert probed == ["KRW-DEAD", "KRW-NPXS"]  # live markets are not probed
    assert b.venue_coverage(result)["delisted_pairs"] == 1


def test_snapshot_not_after_coverage_end_is_refused(
    archive: ArchiveFixture, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="not after coverage_end"):
        b.build_binance(
            archive, tmp_path, _exchange_info(),
            snapshot_date=date(2025, 1, 31), cache_dir=None,
        )  # fmt: skip


def test_interrupted_cache_write_is_refetched_not_fatal(
    archive: ArchiveFixture, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    first = ba.fetch_month(archive, "DEADUSDT", "2024-11", cache)
    checksum = cache / "binance_spot/1d/DEADUSDT/DEADUSDT-1d-2024-11.zip.CHECKSUM"
    checksum.write_text(checksum.read_text()[:10])  # torn write
    archive.requests.clear()
    assert ba.fetch_month(archive, "DEADUSDT", "2024-11", cache) == first
    assert any(u.endswith("2024-11.zip") for u in archive.requests)
    archive.requests.clear()
    assert ba.fetch_month(archive, "DEADUSDT", "2024-11", cache) == first
    assert archive.requests == []  # cache healed


def test_bars_outside_the_archive_month_are_rejected(tmp_path: Path) -> None:
    fx = ArchiveFixture()
    fx.add_month("BTCUSDT", "2024-11",
                 _kline_rows(date(2024, 12, 1), date(2024, 12, 2), micro=False))  # fmt: skip
    with pytest.raises(ba.ArchiveError, match="outside 2024-11"):
        ba.fetch_month(fx, "BTCUSDT", "2024-11")


def test_non_ascii_symbols_are_percent_encoded() -> None:
    url = ba.month_zip_url("牛来USDT", "2026-01")
    assert url.isascii()
    check_url(url)
    assert urllib.parse.unquote(url).endswith("/牛来USDT/1d/牛来USDT-1d-2026-01.zip")


def test_unexpected_worker_exception_is_recorded_per_pair(tmp_path: Path) -> None:
    fx = ArchiveFixture()
    fx.add_month(
        "BTCUSDT",
        "2024-11",
        _kline_rows(date(2024, 11, 1), date(2024, 11, 30), micro=False),
    )
    fx.add_month(
        "ODDUSDT", "2024-11", "1730419200000,1,2,0.5,1.5,10,1,15,notanint,5,7,0\n"
    )
    result = b.build_binance(
        fx, tmp_path, _exchange_info(), snapshot_date=SNAP, cache_dir=None
    )
    assert [x.symbol for x in result.listings] == ["BTCUSDT"]
    assert result.errors[0]["symbol"] == "ODDUSDT"  # fmt: skip


@pytest.mark.parametrize(
    "url",
    [
        "https://api.upbit.com/v1/candles/days/../../orders",
        "https://api.upbit.com/v1/market/all/../../accounts",
        "https://api.upbit.com/v1/candles/days%2F..%2F..%2Forders",
        "https://api.upbit.com/v1/candles/daysX",
        "https://data-api.binance.vision/api/v3/exchangeInfo/../account",
        "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision-attacker-bucket/x",
        "https://data.binance.vision/data/spot/../futures/um/x.zip",
        "https://data.binance.vision/data/spot/%2e%2e/futures/x.zip",
    ],
)
def test_fetcher_refuses_dot_segments_and_prefix_tricks(url: str) -> None:
    with pytest.raises(DisallowedUrlError):
        check_url(url)


def test_pit_cli_refuses_a_table_that_does_not_match_the_manifest(
    archive: ArchiveFixture, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from research.crypto_universe import cli

    result = b.build_binance(
        archive, tmp_path, _exchange_info(), snapshot_date=SNAP, cache_dir=None
    )
    b.write_outputs(tmp_path, {"binance_usdt_spot": result}, {})
    args = ["pit", "--root", str(tmp_path), "--venue", "binance_usdt_spot"]
    assert cli.main([*args, "--date", "2024-12-14"]) == 0
    assert capsys.readouterr().out.split() == ["BTCUSDT", "DEADUSDT"]
    assert cli.main([*args, "--date", "2025-02-01"]) == 2  # after coverage_end
    table = tmp_path / "universe/pit_universe_binance_usdt_spot.csv"
    table.write_text(table.read_text().replace("2024-12-14", "2024-12-20"))
    assert cli.main([*args, "--date", "2024-12-14"]) == 2
    assert "does not match manifest" in capsys.readouterr().err


def test_identical_duplicate_rows_collapse_but_conflicts_exclude(
    tmp_path: Path,
) -> None:
    fx = ArchiveFixture()
    rows = _kline_rows(date(2024, 11, 1), date(2024, 11, 3), micro=False)
    first = rows.splitlines()[0]
    fx.add_month("BTCUSDT", "2024-11", rows + first + "\n")  # verbatim repeat
    conflict = first.replace(",1.5,", ",1.6,", 1)
    fx.add_month("BADUSDT", "2024-11", rows + conflict + "\n")
    result = b.build_binance(
        fx, tmp_path, _exchange_info(), snapshot_date=SNAP, cache_dir=None
    )
    (btc,) = result.listings
    assert (btc.symbol, btc.trading_days) == ("BTCUSDT", 3)
    assert result.extra["identical_duplicate_rows_dropped"] == 1
    assert result.errors == ({"symbol": "BADUSDT", "month": "*", "error": "conflicting bars for one day"},)  # fmt: skip


def test_pit_cli_refuses_a_venue_missing_from_the_manifest(
    archive: ArchiveFixture, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from research.crypto_universe import cli

    result = b.build_binance(
        archive, tmp_path, _exchange_info(), snapshot_date=SNAP, cache_dir=None
    )
    b.write_outputs(tmp_path, {"binance_usdt_spot": result}, {})
    args = [
        "pit",
        "--root",
        str(tmp_path),
        "--venue",
        "upbit_krw",
        "--date",
        "2024-12-01",
    ]
    assert cli.main(args) == 2
    assert "no upbit_krw table" in capsys.readouterr().err


def test_failed_rebuild_keeps_the_previous_outputs(tmp_path: Path) -> None:
    from research.crypto_universe import cli
    from research.crypto_universe.snapshot import SnapshotError

    for name in ("candles", "universe"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "keep.txt").write_text(name)
    # no snapshot exists, so the build fails after staging, before promotion
    with pytest.raises(SnapshotError):
        cli.main(["build", "--root", str(tmp_path), "--replace"])
    for name in ("candles", "universe"):
        assert (tmp_path / name / "keep.txt").read_text() == name
    with pytest.raises(SystemExit, match="--replace"):
        cli.main(["build", "--root", str(tmp_path)])


def test_promote_staging_swaps_outputs(tmp_path: Path) -> None:
    from research.crypto_universe.cli import promote_staging

    (tmp_path / "universe").mkdir()
    (tmp_path / "universe" / "old.txt").write_text("old")
    staging = tmp_path / ".build-staging"
    (staging / "universe").mkdir(parents=True)
    (staging / "universe" / "new.txt").write_text("new")
    (staging / "candles").mkdir()
    promote_staging(tmp_path, staging, ("candles", "universe"))
    assert sorted(p.name for p in (tmp_path / "universe").iterdir()) == ["new.txt"]
    assert (tmp_path / "candles").is_dir()
    assert not staging.exists() and not (tmp_path / ".build-previous").exists()


def _outputs(root: Path, label: str) -> None:
    for name in ("candles", "universe"):
        (root / name).mkdir(parents=True)
        (root / name / f"{label}.txt").write_text(label)


def _labels(root: Path) -> dict[str, list[str]]:
    return {
        name: sorted(p.name for p in (root / name).iterdir())
        for name in ("candles", "universe")
        if (root / name).exists()
    }


@pytest.mark.parametrize("crash_at_rename", [1, 2, 3, 4])
def test_crash_during_promotion_never_loses_the_previous_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_at_rename: int
) -> None:
    from research.crypto_universe import cli

    names = ("candles", "universe")
    _outputs(tmp_path, "old")
    staging = tmp_path / ".build-staging"
    _outputs(staging, "new")
    real_rename = Path.rename
    calls = {"n": 0}

    def crashing_rename(self: Path, target: Path) -> Path:
        calls["n"] += 1
        if calls["n"] == crash_at_rename:
            raise KeyboardInterrupt("simulated crash")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", crashing_rename)
    with pytest.raises(KeyboardInterrupt):
        cli.promote_staging(tmp_path, staging, names)
    monkeypatch.setattr(Path, "rename", real_rename)

    # a later build start runs recovery first: the old outputs come back whole
    cli.recover_interrupted_promotion(tmp_path, names)
    assert _labels(tmp_path) == {"candles": ["old.txt"], "universe": ["old.txt"]}
    assert not (tmp_path / cli.PREVIOUS_DIR).exists()
    assert cli.recover_interrupted_promotion(tmp_path, names) == []


def test_crash_after_promotion_marker_keeps_the_new_outputs(tmp_path: Path) -> None:
    from research.crypto_universe import cli

    names = ("candles", "universe")
    _outputs(tmp_path, "new")
    previous = tmp_path / cli.PREVIOUS_DIR
    _outputs(previous, "old")
    (previous / cli.OLDS_SAVED_MARKER).touch()
    (previous / cli.PROMOTED_MARKER).touch()
    cli.recover_interrupted_promotion(tmp_path, names)
    assert _labels(tmp_path) == {"candles": ["new.txt"], "universe": ["new.txt"]}
    assert not previous.exists()


def test_build_start_recovers_before_anything_else(tmp_path: Path) -> None:
    from research.crypto_universe import cli
    from research.crypto_universe.snapshot import SnapshotError

    previous = tmp_path / cli.PREVIOUS_DIR
    _outputs(previous, "old")  # crash right after saving, nothing promoted
    with pytest.raises(SnapshotError):  # no snapshot; fails after recovery
        cli.main(["build", "--root", str(tmp_path), "--replace"])
    assert _labels(tmp_path) == {"candles": ["old.txt"], "universe": ["old.txt"]}
    assert not previous.exists()
