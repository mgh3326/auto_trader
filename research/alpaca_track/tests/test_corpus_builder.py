"""ROB-1059 H1 (spec §14.1/AC1/AC3) — spot corpus builder: fetch -> checksum ->
normalize -> manifest, with month -> day -> REST-backfill fallback.

Fully fixture-driven via fake in-memory openers: NO real network calls
anywhere in this suite (the hard constraint for this issue — the real
corpus collection run is operator-approved and happens later). Exercises the
fail-closed checksum chain and the exact three-tier source fallback the
manifest must distinguish.
"""

import hashlib
import io
import json
import zipfile

import corpus_builder as cb
import pytest
import rob941_archive_fetch as af
import spot_archive_fetch as saf

HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"


def _kline_line(open_time_ms: int, close: float = 100.5) -> str:
    close_time = open_time_ms + 59_999
    return f"{open_time_ms},100.0,101.0,99.0,{close},10.0,{close_time},1000.0,5,4.0,400.0,0"


def _month_csv(start_ms: int, n_minutes: int) -> str:
    lines = [HEADER]
    for m in range(n_minutes):
        lines.append(_kline_line(start_ms + m * 60_000) + "\n")
    return "".join(lines)


def _zip_and_checksum(name: str, content: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    zb = buf.getvalue()
    checksum = f"{hashlib.sha256(zb).hexdigest()}  {name}\n".encode()
    return zb, checksum


def _rest_json_bytes(start_ms: int, n_minutes: int) -> bytes:
    rows = []
    for m in range(n_minutes):
        ts = start_ms + m * 60_000
        rows.append(
            [
                ts,
                "100.0",
                "101.0",
                "99.0",
                "100.5",
                "10.0",
                ts + 59_999,
                "1000.0",
                5,
                "4.0",
                "400.0",
                "0",
            ]
        )
    return json.dumps(rows).encode()


class _FakeUniverse:
    """Populates a fake archive table for one symbol/month with monthly,
    daily, and REST tiers independently controllable per test."""

    def __init__(self, symbol: str, year: int, month: int, n_minutes: int = 10):
        self.symbol = symbol
        self.year = year
        self.month = month
        self.n_minutes = n_minutes
        self.month_start_ms, _ = saf.month_bounds_ms(year, month)
        self.archive_table: dict[str, bytes] = {}
        self.rest_table: dict[str, bytes] = {}

    def with_monthly_archive(self) -> "_FakeUniverse":
        text = _month_csv(self.month_start_ms, self.n_minutes)
        name = f"{self.symbol}-1m-{self.year:04d}-{self.month:02d}.csv"
        zb, chk = _zip_and_checksum(name, text)
        url = saf.spot_kline_monthly_url(self.symbol, "1m", self.year, self.month)
        self.archive_table[url] = zb
        self.archive_table[url + ".CHECKSUM"] = chk
        return self

    def with_daily_archive_for_day1(self) -> "_FakeUniverse":
        text = _month_csv(self.month_start_ms, self.n_minutes)
        name = f"{self.symbol}-1m-{self.year:04d}-{self.month:02d}-01.csv"
        zb, chk = _zip_and_checksum(name, text)
        url = saf.spot_kline_daily_url(self.symbol, "1m", self.year, self.month, 1)
        self.archive_table[url] = zb
        self.archive_table[url + ".CHECKSUM"] = chk
        return self

    def with_rest_backfill_for_day1(self) -> "_FakeUniverse":
        body = _rest_json_bytes(self.month_start_ms, self.n_minutes)
        url = saf.rest_klines_url(
            self.symbol,
            "1m",
            self.month_start_ms,
            self.month_start_ms + self.n_minutes * 60_000,
        )
        self.rest_table[url] = body
        return self

    def archive_opener(self, url: str):
        return self.archive_table.get(url)

    def rest_opener(self, url: str):
        return self.rest_table.get(url)


def _window(year: int, month: int, n_minutes: int) -> tuple[int, int]:
    start, _ = saf.month_bounds_ms(year, month)
    return start, start + n_minutes * 60_000


def test_monthly_archive_is_used_when_available_source_is_archive_monthly():
    fake = _FakeUniverse("BTCUSDC", 2024, 6).with_monthly_archive()
    start, end = _window(2024, 6, 10)
    rows, manifest = cb.build_symbol_corpus(
        "BTCUSDC",
        "USDC",
        start,
        end,
        archive_opener=fake.archive_opener,
        rest_opener=fake.rest_opener,
    )
    assert len(rows) == 10
    assert manifest.row_count == 10
    assert manifest.expected_count == 10
    assert manifest.missing_open_times_ms == ()
    assert len(manifest.sources) == 1
    assert manifest.sources[0].source == "archive_monthly"
    assert manifest.sources[0].checksum_sha256 is not None


def test_falls_back_to_daily_archive_when_monthly_missing():
    fake = _FakeUniverse("XRPUSDC", 2024, 6).with_daily_archive_for_day1()
    start, end = _window(2024, 6, 10)
    rows, manifest = cb.build_symbol_corpus(
        "XRPUSDC",
        "USDC",
        start,
        end,
        archive_opener=fake.archive_opener,
        rest_opener=fake.rest_opener,
    )
    assert len(rows) == 10
    day_sources = [s for s in manifest.sources if s.source == "archive_daily"]
    assert len(day_sources) == 1
    assert day_sources[0].day == 1
    assert day_sources[0].checksum_sha256 is not None
    assert all(s.source != "backfill_rest" for s in manifest.sources)


def test_falls_back_to_rest_backfill_when_neither_monthly_nor_daily_archive_exists():
    fake = _FakeUniverse("DOGEUSDC", 2024, 6).with_rest_backfill_for_day1()
    start, end = _window(2024, 6, 10)
    rows, manifest = cb.build_symbol_corpus(
        "DOGEUSDC",
        "USDC",
        start,
        end,
        archive_opener=fake.archive_opener,
        rest_opener=fake.rest_opener,
    )
    assert len(rows) == 10
    backfill_sources = [s for s in manifest.sources if s.source == "backfill_rest"]
    assert len(backfill_sources) >= 1
    assert all(s.checksum_sha256 is None for s in backfill_sources)


def test_source_is_distinguished_never_conflated_across_tiers():
    # month 1 has a monthly archive; the daily-only day-1 fallback and REST
    # fallback are exercised in separate symbols above, but this test asserts
    # the ShardSource dataclass itself refuses to conflate tiers.
    from corpus_manifest import ShardSource

    with pytest.raises(ValueError):
        ShardSource(
            source="archive_monthly",
            year=2024,
            month=6,
            day=None,
            url="https://example/",
            checksum_sha256=None,  # archive tiers MUST carry a checksum
        )
    with pytest.raises(ValueError):
        ShardSource(
            source="backfill_rest",
            year=2024,
            month=6,
            day=1,
            url="https://example/",
            checksum_sha256="deadbeef",  # backfill MUST NOT carry a checksum
        )


def test_missing_checksum_sidecar_fails_closed_not_silently_downgraded_to_rest():
    # AC1 remediation: the archive bytes are present but its mandatory
    # ".CHECKSUM" sidecar is ABSENT. This must raise ChecksumMissingError and
    # propagate -- the builder's `except af.ArchiveMissingError:` fallback
    # clause must NOT also catch ChecksumMissingError (a mutation that widened
    # that except clause to catch it too would silently downgrade to an
    # unchecksummed REST backfill instead of failing closed, exactly what AC1
    # forbids). No prior test in this suite ever exercised "archive present,
    # checksum sidecar absent" -- every existing fixture always sets both
    # keys together.
    fake = _FakeUniverse("ETCUSDC", 2024, 6).with_monthly_archive()
    checksum_key = next(k for k in fake.archive_table if k.endswith(".CHECKSUM"))
    del fake.archive_table[checksum_key]  # archive bytes present, sidecar absent
    start, end = _window(2024, 6, 10)
    with pytest.raises(af.ChecksumMissingError):
        cb.build_symbol_corpus(
            "ETCUSDC",
            "USDC",
            start,
            end,
            archive_opener=fake.archive_opener,
            rest_opener=fake.rest_opener,
        )


def test_daily_fallback_missing_checksum_sidecar_also_fails_closed():
    # same guard, but on the daily-archive fallback path (_fill_via_daily_
    # then_rest) rather than the monthly path.
    fake = _FakeUniverse("ADAUSDC", 2024, 6).with_daily_archive_for_day1()
    checksum_key = next(k for k in fake.archive_table if k.endswith(".CHECKSUM"))
    del fake.archive_table[checksum_key]
    start, end = _window(2024, 6, 10)
    with pytest.raises(af.ChecksumMissingError):
        cb.build_symbol_corpus(
            "ADAUSDC",
            "USDC",
            start,
            end,
            archive_opener=fake.archive_opener,
            rest_opener=fake.rest_opener,
        )


def test_checksum_mismatch_fails_closed():
    fake = _FakeUniverse("SOLUSDC", 2024, 6).with_monthly_archive()
    checksum_key = next(k for k in fake.archive_table if k.endswith(".CHECKSUM"))
    fake.archive_table[checksum_key] = b"0" * 64 + b"  corrupt.csv\n"
    start, end = _window(2024, 6, 10)
    with pytest.raises(af.ChecksumMismatchError):
        cb.build_symbol_corpus(
            "SOLUSDC",
            "USDC",
            start,
            end,
            archive_opener=fake.archive_opener,
            rest_opener=fake.rest_opener,
        )


def test_manifest_is_deterministic_on_rerun_without_new_collection():
    fake = _FakeUniverse("BCHUSDC", 2024, 6).with_monthly_archive()
    start, end = _window(2024, 6, 10)
    _, manifest1 = cb.build_symbol_corpus(
        "BCHUSDC",
        "USDC",
        start,
        end,
        archive_opener=fake.archive_opener,
        rest_opener=fake.rest_opener,
    )
    _, manifest2 = cb.build_symbol_corpus(
        "BCHUSDC",
        "USDC",
        start,
        end,
        archive_opener=fake.archive_opener,
        rest_opener=fake.rest_opener,
    )
    assert manifest1.normalized_content_sha256 == manifest2.normalized_content_sha256
    assert manifest1.to_dict() == manifest2.to_dict()


def test_gap_is_recorded_when_archive_has_fewer_minutes_than_expected():
    fake = _FakeUniverse("LTCUSDC", 2024, 6, n_minutes=5).with_monthly_archive()
    start, end = _window(2024, 6, 10)  # expect 10 minutes, only 5 present
    rows, manifest = cb.build_symbol_corpus(
        "LTCUSDC",
        "USDC",
        start,
        end,
        archive_opener=fake.archive_opener,
        rest_opener=fake.rest_opener,
    )
    assert manifest.row_count == 5
    assert manifest.expected_count == 10
    assert len(manifest.missing_open_times_ms) == 5


def test_corpus_builder_module_has_no_broker_order_or_db_imports():
    import ast
    from pathlib import Path

    src = Path(cb.__file__).read_text()
    tree = ast.parse(src)
    forbidden = {"app", "sqlalchemy", "asyncpg", "taskiq", "random"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [n.name.split(".")[0] for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        assert not (set(names) & forbidden), (
            f"forbidden import in corpus_builder: {names}"
        )
