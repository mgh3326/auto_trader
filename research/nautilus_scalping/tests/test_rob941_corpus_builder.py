"""ROB-941 — corpus builder orchestration (fetch -> extract -> normalize -> gap-detect).

Fully fixture-driven via a fake in-memory opener: no network in the default suite.
Exercises the fail-closed chain end-to-end (a bad checksum anywhere in the window
aborts the whole symbol build) and confirms zero DB/broker/scheduler code paths
exist by construction (this module imports only rob941_* + funding_oi_archive).
"""

import hashlib
import io
import zipfile

import pytest
import rob941_archive_fetch as af
import rob941_corpus_builder as cb
import rob941_frozen_scope as scope

HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
FUNDING_HEADER = "calc_time,funding_interval_hours,last_funding_rate\n"


def _kline_row(open_time_ms: int) -> str:
    close_time = open_time_ms + 59_999
    return (
        f"{open_time_ms},100.0,101.0,99.0,100.5,10.0,{close_time},1000.0,5,4.0,400.0,0"
    )


def _month_csv(
    symbol: str, start_ms: int, n_minutes: int, skip_offsets=frozenset()
) -> str:
    lines = [HEADER]
    for m in range(n_minutes):
        if m in skip_offsets:
            continue
        lines.append(_kline_row(start_ms + m * 60_000) + "\n")
    return "".join(lines)


def _zip_and_checksum(name: str, content: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    zb = buf.getvalue()
    checksum = f"{hashlib.sha256(zb).hexdigest()}  {name}\n".encode()
    return zb, checksum


class _FakeCorpusUniverse:
    """Populates all 12 frozen-window months with a tiny 5-bar fixture each (a
    real fetch would be a full month of 1m bars; the builder doesn't care about
    volume, only about the fetch/checksum/parse/gap-detect chain). Every fake
    month's 5 bars sit at a distinct minute offset from ``WINDOW_START_MS`` so
    cross-month timestamps never collide; the second month drops one bar to
    prove ``build_symbol_kline_shard`` surfaces a real, LOCATABLE gap (not just
    "some gap exists somewhere in the year")."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.table: dict[str, bytes] = {}
        self.second_month_gap_start_ms: int | None = None
        self._populate()

    def _populate(self):
        months = scope.months_in_window()
        for idx, (year, month) in enumerate(months):
            month_start = (
                scope.WINDOW_START_MS + idx * 10 * 60_000
            )  # 10-min spacing, no overlap
            url = af.kline_archive_url(self.symbol, "1m", year, month)
            csv_name = f"{self.symbol}-1m-{year:04d}-{month:02d}.csv"
            # each fake "month" is tiny: 5 one-minute bars, second month skips 1 bar (a gap)
            skip = {2} if idx == 1 else frozenset()
            if idx == 1:
                self.second_month_gap_start_ms = month_start + 2 * 60_000
            text = _month_csv(self.symbol, month_start, 5, skip_offsets=skip)
            zb, chk = _zip_and_checksum(csv_name, text)
            self.table[url] = zb
            self.table[url + ".CHECKSUM"] = chk

            fund_url = af.funding_archive_url(self.symbol, year, month)
            fund_name = f"{self.symbol}-fundingRate-{year:04d}-{month:02d}.csv"
            fund_text = FUNDING_HEADER + f"{month_start},8,0.0001{idx}\n"
            fzb, fchk = _zip_and_checksum(fund_name, fund_text)
            self.table[fund_url] = fzb
            self.table[fund_url + ".CHECKSUM"] = fchk

    def opener(self, url: str):
        return self.table.get(url)


def test_build_symbol_kline_shard_normalizes_and_detects_gap_from_fixture():
    fake = _FakeCorpusUniverse("XRPUSDT")
    rows, provenance, gap_ranges = cb.build_symbol_kline_shard(
        "XRPUSDT", opener=fake.opener
    )
    assert all(r.symbol == "XRPUSDT" for r in rows)
    assert (
        len(provenance) == 12
    )  # one entry per frozen-window month, all checksum-verified
    # 11 months x 5 bars + 1 month x 4 bars (one skipped) = 59 real bars
    assert len(rows) == 59
    gap_start = fake.second_month_gap_start_ms
    assert any(g0 == gap_start and g1 == gap_start + 60_000 for g0, g1 in gap_ranges), (
        f"expected an exact 1-minute gap range at {gap_start}, got {gap_ranges[:5]}... "
        f"({len(gap_ranges)} total ranges)"
    )


def test_build_symbol_kline_shard_fails_closed_on_checksum_mismatch():
    fake = _FakeCorpusUniverse("XRPUSDT")
    # corrupt exactly one month's checksum
    any_checksum_key = next(
        k for k in fake.table if k.endswith(".CHECKSUM") and "klines" in k
    )
    fake.table[any_checksum_key] = b"0" * 64 + b"  corrupt.csv\n"
    with pytest.raises(af.ChecksumMismatchError):
        cb.build_symbol_kline_shard("XRPUSDT", opener=fake.opener)


def test_build_symbol_funding_shard_normalizes_from_fixture():
    fake = _FakeCorpusUniverse("DOGEUSDT")
    rows, provenance = cb.build_symbol_funding_shard("DOGEUSDT", opener=fake.opener)
    assert len(rows) == 12  # one funding row per fake month
    assert len(provenance) == 12


def test_build_symbol_kline_shard_invokes_raw_archive_sink_once_per_month_with_verified_bytes():
    fake = _FakeCorpusUniverse("XRPUSDT")
    calls = []

    def sink(symbol, kind, year, month, zip_bytes):
        calls.append((symbol, kind, year, month, zip_bytes))
        return f"rob941/raw/{kind}/{symbol}/{symbol}-{kind}-{year:04d}-{month:02d}.zip"

    rows, provenance, gap_ranges = cb.build_symbol_kline_shard(
        "XRPUSDT", opener=fake.opener, raw_archive_sink=sink
    )
    assert len(calls) == 12  # one per frozen-window month, never skipped
    assert all(c[0] == "XRPUSDT" and c[1] == "klines" for c in calls)
    assert all(isinstance(c[4], bytes) and len(c[4]) > 0 for c in calls)
    # the sink's returned relative path must land on the archive's provenance
    assert all(p.local_path is not None for p in provenance)
    assert all(
        p.local_path.startswith("rob941/raw/klines/XRPUSDT/") for p in provenance
    )


def test_build_symbol_kline_shard_without_sink_leaves_local_path_none():
    fake = _FakeCorpusUniverse("XRPUSDT")
    rows, provenance, gap_ranges = cb.build_symbol_kline_shard(
        "XRPUSDT", opener=fake.opener
    )
    assert all(p.local_path is None for p in provenance)


def test_build_symbol_funding_shard_invokes_raw_archive_sink_with_funding_rate_kind():
    fake = _FakeCorpusUniverse("DOGEUSDT")
    calls = []

    def sink(symbol, kind, year, month, zip_bytes):
        calls.append((symbol, kind, year, month))
        return f"rob941/raw/{kind}/{symbol}/{symbol}-{kind}-{year:04d}-{month:02d}.zip"

    rows, provenance = cb.build_symbol_funding_shard(
        "DOGEUSDT", opener=fake.opener, raw_archive_sink=sink
    )
    assert len(calls) == 12
    assert all(c[1] == "fundingRate" for c in calls)
    assert all(p.local_path is not None for p in provenance)


def test_corpus_builder_module_has_no_broker_order_or_db_imports():
    # zero broker/order/fill/scheduler wiring — enforced structurally, not by convention
    import ast
    from pathlib import Path

    src = Path(cb.__file__).read_text()
    tree = ast.parse(src)
    forbidden = {"app", "sqlalchemy", "asyncpg", "taskiq"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [n.name.split(".")[0] for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        assert not (set(names) & forbidden), (
            f"forbidden import in corpus builder: {names}"
        )
