"""Invariant tests for us-intraday-corpus-v1.

The three §0 pitfalls that got the daily sister corpus BLOCKED each get a test
that FAILS if the pitfall is reintroduced. These are regression tests for
mistakes that have already happened once.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from research.us_intraday_corpus import (
    access_log,
    alpaca_data,
    bars,
    build,
    config,
    finalize,
    hashing,
    labels,
    loader,
    writer,
)


@pytest.fixture()
def artifact_root(tmp_path, monkeypatch):
    """Redirect every artifact path into a temp dir."""
    root = tmp_path / "corpus"
    monkeypatch.setattr(config, "ARTIFACT_ROOT", root)
    monkeypatch.setattr(config, "DATASET_DIR", root / "dataset")
    monkeypatch.setattr(config, "HOLDOUT_DIR", root / "holdout")
    monkeypatch.setattr(config, "REPORTS_DIR", root / "reports")
    monkeypatch.setattr(config, "INPUTS_DIR", root / "inputs")
    monkeypatch.setattr(config, "STAGING_DIR", root / "_staging")
    monkeypatch.setattr(config, "MANIFEST_PATH", root / "manifest.json")
    monkeypatch.setattr(config, "CHECKSUMS_PATH", root / "checksums.sha256")
    monkeypatch.setattr(config, "ACCESS_LOG_PATH", root / "holdout-access.log")
    monkeypatch.setattr(finalize, "REGISTRY_PATH", root / "_staging" / "wt.json")
    finalize.WRITE_TIME_DIGESTS.clear()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _table() -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                "symbol": "AAPL",
                "ts_utc": _dt.datetime(2025, 3, 4, 15, 0, tzinfo=_dt.UTC),
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 100,
            }
        ]
    )


# --------------------------------------------------------------------------
# §0 pitfall 1 -- the holdout must be hashed at write and never re-read
# --------------------------------------------------------------------------


def test_holdout_digest_comes_from_write_not_reread(artifact_root):
    path = config.HOLDOUT_DIR / "freq=1h" / "year=2025" / "AAPL.parquet"
    result = writer.write_parquet_atomic(_table(), path)

    # The digest must describe the shipped bytes without any read-back.
    assert result.sha256 == hashing.sha256_of_bytes(path.read_bytes())

    # And hashing a holdout file by reading it is refused outright.
    with pytest.raises(access_log.HoldoutGuardViolation, match="refusing to read"):
        hashing.sha256_of_file(path)


def test_posthoc_walk_excludes_holdout(artifact_root):
    """This is the exact bug: rglob('*.parquet') swept the sealed holdout in."""
    explor = config.DATASET_DIR / "freq=1h" / "year=2024" / "AAPL.parquet"
    held = config.HOLDOUT_DIR / "freq=1h" / "year=2025" / "AAPL.parquet"
    writer.write_parquet_atomic(_table(), explor)
    writer.write_parquet_atomic(_table(), held)

    walked = finalize._shippable_files()
    assert explor in walked
    assert held not in walked
    assert not any(access_log.is_holdout_path(p) for p in walked)


def test_access_log_records_reads_not_only_writes(artifact_root):
    """The sister corpus's log had WRITE lines only, so it could not catch a read."""
    path = config.HOLDOUT_DIR / "freq=1h" / "year=2025" / "AAPL.parquet"
    writer.write_parquet_atomic(_table(), path)
    assert access_log.verify_written_not_read() is True

    with access_log.guarded_open(path, "rb") as handle:
        handle.read(16)

    modes = [m for _ts, m, _p in access_log.read_records()]
    assert "READ" in modes, "a holdout read must appear in the access log"


def test_written_not_read_claim_is_derived_and_flips_on_a_read(artifact_root):
    """`written_not_read` must be computed from evidence, never asserted."""
    path = config.HOLDOUT_DIR / "freq=1h" / "year=2025" / "AAPL.parquet"
    result = writer.write_parquet_atomic(_table(), path)
    finalize.register_digest(path, result.sha256)

    manifest = finalize.seal(terminal_verdict="BUILT_WITH_GAPS", body={})
    assert manifest["holdout"]["written_not_read"] is True

    with access_log.guarded_open(path, "rb") as handle:
        handle.read(1)

    manifest2 = finalize.seal(terminal_verdict="BUILT_WITH_GAPS", body={})
    assert manifest2["holdout"]["written_not_read"] is False, (
        "after a holdout read the manifest must stop claiming written_not_read"
    )


def test_holdout_digest_still_appears_in_checksums(artifact_root):
    """Sealed but not unlisted: the holdout is covered by integrity checks."""
    path = config.HOLDOUT_DIR / "freq=1h" / "year=2025" / "AAPL.parquet"
    result = writer.write_parquet_atomic(_table(), path)
    finalize.register_digest(path, result.sha256)

    body, stats = finalize.build_checksums()
    assert result.sha256 in body
    assert stats["write_time_hashed"] == 1


def test_every_enumerated_bypass_surface_is_blocked(artifact_root, monkeypatch):
    """The full alias surface, enumerated rather than patched one form at a time.

    Three rounds of review each found one more alias (`..`, then case/symlink,
    then hardlink) because the guard was being fixed reactively. This test
    enumerates the surface and asserts every entry raises, so a future
    regression shows up as a named failure rather than a fresh discovery.

    Hardlinks are why identity is anchored on the inode: a second directory
    entry outside holdout/ points at the same inode, and no amount of
    realpath/casefold work on the NAME can see that.
    """
    import os
    import unicodedata

    monkeypatch.setattr(config, "SISTER_HOLDOUT_DIR", artifact_root / "no-such-sister")
    hold = config.HOLDOUT_DIR / "freq=1m"
    hold.mkdir(parents=True, exist_ok=True)
    sealed = hold / "p.parquet"
    sealed.write_bytes(b"SEALED")
    config.DATASET_DIR.mkdir(parents=True, exist_ok=True)
    access_log.refresh_holdout_inodes()

    os.symlink(config.HOLDOUT_DIR, artifact_root / "lnk")
    os.symlink(artifact_root / "lnk", artifact_root / "lnk2")
    os.link(sealed, artifact_root / "hard.parquet")
    os.link(sealed, config.DATASET_DIR / "inside_dataset.parquet")
    nfd = artifact_root / unicodedata.normalize("NFD", "hőldout_link")
    os.symlink(config.HOLDOUT_DIR, nfd)

    surfaces = {
        "exact": sealed,
        "dot_component": config.HOLDOUT_DIR / "." / "freq=1m" / "p.parquet",
        "dotdot": config.DATASET_DIR / ".." / "holdout" / "freq=1m" / "p.parquet",
        "redundant_separators": Path(
            str(artifact_root) + "//holdout///freq=1m//p.parquet"
        ),
        "uppercase": Path(str(sealed).replace("/holdout/", "/HOLDOUT/")),
        "mixed_case": Path(str(sealed).replace("/holdout/", "/HoLdOuT/")),
        "symlink": artifact_root / "lnk" / "freq=1m" / "p.parquet",
        "nested_symlink": artifact_root / "lnk2" / "freq=1m" / "p.parquet",
        "dotdot_through_symlink": artifact_root
        / "lnk"
        / ".."
        / "holdout"
        / "freq=1m"
        / "p.parquet",
        "nfd_symlink": nfd / "freq=1m" / "p.parquet",
        "hardlink_outside_holdout": artifact_root / "hard.parquet",
        "hardlink_inside_dataset": config.DATASET_DIR / "inside_dataset.parquet",
        "relative": Path(os.path.relpath(sealed, os.getcwd())),
    }
    leaks = []
    for name, path in surfaces.items():
        if not access_log.is_holdout_path(path):
            leaks.append(f"{name}: not detected")
            continue
        try:
            hashing.sha256_of_file(path)
            leaks.append(f"{name}: hash guard leaked")
        except access_log.HoldoutGuardViolation:
            pass
        except OSError:
            pass
        try:
            loader.assert_not_holdout_path(path)
            leaks.append(f"{name}: loader guard leaked")
        except loader.HoldoutAccessDenied:
            pass
    assert not leaks, f"holdout guard bypassed by: {leaks}"

    # Must not over-block: an ordinary dataset file stays readable.
    ok = config.DATASET_DIR / "normal.parquet"
    ok.write_bytes(b"open")
    assert access_log.is_holdout_path(ok) is False
    assert hashing.sha256_of_file(ok)


def test_hardlink_made_after_cache_build_is_still_caught(artifact_root, monkeypatch):
    """A hardlink created after the inode cache was built must not slip through.

    `st_nlink > 1` forces a cache rebuild before a negative is trusted, so the
    guard cannot be defeated by racing the cache.
    """
    import os

    monkeypatch.setattr(config, "SISTER_HOLDOUT_DIR", artifact_root / "no-sister")
    hold = config.HOLDOUT_DIR / "freq=1m"
    hold.mkdir(parents=True, exist_ok=True)
    sealed = hold / "p.parquet"
    sealed.write_bytes(b"SEALED")

    access_log.refresh_holdout_inodes()  # cache built BEFORE the alias exists
    alias = artifact_root / "late.parquet"
    os.link(sealed, alias)

    assert access_log.is_holdout_path(alias)
    with pytest.raises(access_log.HoldoutGuardViolation):
        hashing.sha256_of_file(alias)


def test_case_and_symlink_forms_cannot_bypass_the_holdout_guard(
    artifact_root, monkeypatch
):
    """Regression: `abspath` let two forms through the seal.

    Cross-series verification found that `HOLDOUT/…` (uppercase, the same
    directory on a case-insensitive filesystem) and `link -> holdout/…` both
    returned False from `is_holdout_path`, so the hash guard and the loader
    guard were bypassable. Detection must be symlink-resolved and
    case-insensitive, and it must RAISE -- a silent skip is indistinguishable
    from a guard that was never reached.
    """
    import os

    hold = config.HOLDOUT_DIR / "freq=1m"
    hold.mkdir(parents=True, exist_ok=True)
    target = hold / "p.parquet"
    target.write_bytes(b"sealed")
    config.DATASET_DIR.mkdir(parents=True, exist_ok=True)

    link = artifact_root / "sneaky"
    os.symlink(config.HOLDOUT_DIR, link)

    forms = {
        "exact": target,
        "dotdot": config.DATASET_DIR / ".." / "holdout" / "freq=1m" / "p.parquet",
        "uppercase": artifact_root / "HOLDOUT" / "freq=1m" / "p.parquet",
        "symlink": link / "freq=1m" / "p.parquet",
    }
    for name, path in forms.items():
        assert access_log.is_holdout_path(path), f"{name} form bypassed detection"
        with pytest.raises(access_log.HoldoutGuardViolation):
            hashing.sha256_of_file(path)
        with pytest.raises(loader.HoldoutAccessDenied):
            loader.assert_not_holdout_path(path)

    # A non-holdout path must remain usable -- the guard must not over-block.
    ok = config.DATASET_DIR / "ok.parquet"
    ok.write_bytes(b"open")
    assert access_log.is_holdout_path(ok) is False
    assert hashing.sha256_of_file(ok)


def test_loader_refuses_holdout_on_both_axes():
    with pytest.raises(loader.HoldoutAccessDenied):
        loader.load_holdout()
    with pytest.raises(loader.HoldoutAccessDenied):
        loader.assert_not_holdout_date(_dt.date(2025, 6, 1))
    loader.assert_not_holdout_date(_dt.date(2024, 12, 31))  # boundary is exclusive


# --------------------------------------------------------------------------
# §0 pitfall 2 -- the survivorship label must be enforced, not documented
# --------------------------------------------------------------------------


def test_label_is_embedded_in_parquet_metadata(artifact_root):
    """The sister corpus's parquet carried no label at all."""
    path = config.DATASET_DIR / "freq=1h" / "year=2024" / "AAPL.parquet"
    writer.write_parquet_atomic(_table(), path)

    meta = pq.ParquetFile(str(path)).metadata.metadata
    assert meta[b"SURVIVORSHIP_BIASED"] == b"TRUE"
    assert b"survivorship" in meta[b"survivorship_note"].lower()
    labels.assert_parquet_is_labelled(path)


def test_parquet_label_does_not_clobber_pandas_metadata(artifact_root):
    path = config.DATASET_DIR / "freq=1h" / "year=2024" / "AAPL.parquet"
    writer.write_parquet_atomic(_table(), path)
    meta = pq.ParquetFile(str(path)).metadata.metadata
    assert b"ARROW:schema" in meta


def test_label_appears_in_every_numeric_artifact(artifact_root, tmp_path):
    csv_path = config.REPORTS_DIR / "x.csv"
    labels.write_labelled_csv(
        csv_path, [{"symbol": "AAPL", "n": 1}], fieldnames=["symbol", "n"]
    )
    text = csv_path.read_text()
    assert "SURVIVORSHIP_BIASED" in text.splitlines()[0]
    assert "TRUE" in text.splitlines()[1], (
        "label must be on the data row, not just a header"
    )

    json_path = config.REPORTS_DIR / "x.json"
    labels.write_labelled_json(json_path, {"n": 1})
    assert json.loads(json_path.read_text())["SURVIVORSHIP_BIASED"] == "TRUE"


def test_seal_refuses_unlabelled_artifact(artifact_root):
    """An unlabelled numeric artifact must abort the seal, not warn."""
    rogue = config.REPORTS_DIR / "rogue.csv"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text("symbol,pnl\nAAPL,1.23\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="without the survivorship label"):
        finalize.seal(terminal_verdict="BUILT_WITH_GAPS", body={})


def test_loader_blocks_reads_without_acknowledgement(artifact_root):
    with pytest.raises(loader.SurvivorshipBiasNotAcknowledged):
        loader.load_dataset()


# --------------------------------------------------------------------------
# §0 pitfall 3 -- artifacts must be reproducible from the shipped commit
# --------------------------------------------------------------------------


def test_checksums_cover_reports_not_just_parquet(artifact_root):
    """The stale CSV escaped `us-corpus-v1` because it was not in checksums."""
    writer.write_parquet_atomic(
        _table(), config.DATASET_DIR / "freq=1h" / "year=2024" / "AAPL.parquet"
    )
    labels.write_labelled_csv(
        config.REPORTS_DIR / "ohlc_violation_symbols.csv",
        [{"symbol": "AAPL", "violations": 3}],
        fieldnames=["symbol", "violations"],
    )
    body, _ = finalize.build_checksums()
    assert "reports/ohlc_violation_symbols.csv" in body
    assert "dataset/freq=1h/year=2024/AAPL.parquet" in body


def test_manifest_records_exact_commit_sha(artifact_root):
    manifest = finalize.seal(terminal_verdict="BUILT_WITH_GAPS", body={})
    sha = manifest["generated_from_commit"]
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_seal_rejects_unknown_verdict(artifact_root):
    with pytest.raises(ValueError):
        finalize.seal(terminal_verdict="LOOKS_FINE", body={})


# --------------------------------------------------------------------------
# §4 boundary -- data host only
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets/v2/stocks/bars",
        "https://paper-api.alpaca.markets/v2/orders",
        "https://broker-api.alpaca.markets/v1/accounts",
        "https://evil.example.com/v2/stocks/bars",
        "http://data.alpaca.markets/v2/stocks/bars",
    ],
)
def test_forbidden_hosts_are_blocked(url):
    with pytest.raises(alpaca_data.ForbiddenHostError):
        alpaca_data.assert_data_host(url)


def test_data_host_is_allowed():
    assert (
        alpaca_data.assert_data_host(alpaca_data.BARS_ENDPOINT) == "data.alpaca.markets"
    )


def test_missing_credentials_raise_rather_than_falling_back(monkeypatch, tmp_path):
    for key in (
        "ALPACA_DATA_API_KEY",
        "ALPACA_DATA_API_SECRET",
        "ALPACA_PAPER_API_KEY",
        "ALPACA_PAPER_API_SECRET",
        "ENV_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        alpaca_data, "SANCTIONED_ENV_FILE", str(tmp_path / "absent.native")
    )
    with pytest.raises(alpaca_data.AlpacaCredentialsMissing):
        alpaca_data.load_credentials()


@pytest.mark.parametrize(
    "path",
    [
        "/Users/mgh3326/work/auto_trader/.env.prod",
        "/Users/mgh3326/work/auto_trader/.env.dev",
        "/Users/mgh3326/work/auto_trader/.env",
    ],
)
def test_forbidden_env_files_are_denied(path):
    """orch-mock restricted credentials to one dedicated read-only file."""
    with pytest.raises(alpaca_data.ForbiddenEnvFileError):
        alpaca_data.assert_env_file_allowed(path)


def test_sanctioned_env_file_is_allowed():
    assert alpaca_data.assert_env_file_allowed(alpaca_data.SANCTIONED_ENV_FILE)


def test_credentials_are_read_from_env_file_without_leaking(tmp_path, monkeypatch):
    env = tmp_path / "creds.native"
    env.write_text(
        "ALPACA_PAPER_API_KEY=abc123\nALPACA_PAPER_API_SECRET='sek\"ret'\n# comment\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_FILE", str(env))
    for key in ("ALPACA_DATA_API_KEY", "ALPACA_PAPER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    key, secret = alpaca_data.load_credentials()
    assert key == "abc123"
    assert secret == 'sek"ret'  # quotes stripped, value preserved verbatim


def test_request_counter_enforces_hard_cap():
    counter = alpaca_data.RequestCounter(max_requests=2)
    counter.spend()
    counter.spend()
    with pytest.raises(RuntimeError, match="budget exhausted"):
        counter.spend()


# --------------------------------------------------------------------------
# ROB-1206 -- UTC storage, America/New_York session_date, never KST
# --------------------------------------------------------------------------


def test_session_date_uses_new_york_not_kst():
    """15:00 ET on Jan 2 is already Jan 3 in Seoul -- the one-day-shift trap."""
    instant = _dt.datetime(2024, 1, 2, 20, 0, tzinfo=_dt.UTC)
    assert instant.astimezone(ZoneInfo("America/New_York")).date() == _dt.date(
        2024, 1, 2
    )
    assert instant.astimezone(ZoneInfo("Asia/Seoul")).date() == _dt.date(2024, 1, 3)
    assert bars.session_date(instant) == _dt.date(2024, 1, 2)


def test_kst_anchored_session_date_is_rejected():
    instant = _dt.datetime(2024, 1, 2, 20, 0, tzinfo=_dt.UTC)
    with pytest.raises(AssertionError, match="ROB-1206"):
        bars.assert_no_kst_anchor(instant, _dt.date(2024, 1, 3))


def test_normalize_preserves_utc_instant_verbatim():
    rows = bars.normalize_bars(
        "AAPL",
        [
            {
                "t": "2024-01-02T20:00:00Z",
                "o": 1,
                "h": 2,
                "l": 0.5,
                "c": 1.5,
                "v": 10,
                "n": 2,
                "vw": 1.4,
            }
        ],
    )
    assert rows[0]["ts_utc"] == _dt.datetime(2024, 1, 2, 20, 0, tzinfo=_dt.UTC)
    assert rows[0]["session_date"] == _dt.date(2024, 1, 2)


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError):
        bars.parse_rfc3339_utc("2024-01-02T20:00:00")


# --------------------------------------------------------------------------
# §3.8 / §3.9 -- unfinished bars and page-chain integrity
# --------------------------------------------------------------------------


def test_unfinished_bar_is_not_storable():
    now = _dt.datetime(2024, 1, 2, 20, 30, tzinfo=_dt.UTC)
    open_bar = _dt.datetime(2024, 1, 2, 20, 0, tzinfo=_dt.UTC)
    closed_bar = _dt.datetime(2024, 1, 2, 19, 0, tzinfo=_dt.UTC)
    assert bars.is_finished_bar(open_bar, 60, now) is False
    assert bars.is_finished_bar(closed_bar, 60, now) is True


class _TwoPageClient:
    """Fake client yielding a 2-page chain that terminates on a null token."""

    def __init__(self) -> None:
        self.counter = alpaca_data.RequestCounter()

    def iter_bars(self, symbols, timeframe, start, end):
        chain = alpaca_data.PageChain(symbol=",".join(symbols), timeframe=timeframe)
        for page in range(2):
            chain.pages += 1
            token = "tok" if page == 0 else None
            chain.last_token = token
            day = 4 + page
            payload = {
                "bars": {
                    symbols[0]: [
                        {
                            "t": f"2024-01-0{day}T15:00:00Z",
                            "o": 1.0,
                            "h": 2.0,
                            "l": 0.5,
                            "c": 1.5,
                            "v": 10,
                            "n": 2,
                            "vw": 1.4,
                        }
                    ]
                },
                "next_page_token": token,
            }
            chain.rows += 1
            yield payload, chain
            if not token:
                chain.termination = "null_next_token"
                return


def _old_run_phase_snapshot(client, symbols, timeframe):
    """Replica of the PRE-FIX snapshot placement (inside the consuming loop).

    Kept in the test suite on purpose: it is the fixture that proves the
    assertion below can actually discriminate. Without it, a green test tells
    us nothing about whether the old form would be caught.
    """
    chain_dict = None
    for _payload, chain in client.iter_bars(symbols, timeframe, "s", "e"):
        chain_dict = chain.as_dict()  # <-- premature: termination not set yet
    return chain_dict


@pytest.fixture()
def _single_year_window(monkeypatch):
    monkeypatch.setattr(config, "START_DATE", _dt.date(2024, 1, 1))
    monkeypatch.setattr(config, "CUTOFF_DATE", _dt.date(2024, 12, 31))
    monkeypatch.setattr(
        config, "HOLDOUT", (_dt.date(2025, 1, 1), _dt.date(2026, 7, 31))
    )


def test_run_phase_records_a_completed_page_chain(artifact_root, _single_year_window):
    """Exercises the REAL build.run_phase -- this is the regression guard.

    The previous version of this test built its own generator and never touched
    `build.run_phase`, so it stayed green even when `run_phase` was reverted to
    the broken form. It therefore protected nothing. This one drives the actual
    production call site, so a regression in snapshot placement fails it.
    """
    stats = build.run_phase(_TwoPageClient(), ["AAPL"], "1Min", "1m", 1)

    assert stats.page_chains, "run_phase must record a page chain"
    chain = stats.page_chains[0]
    assert chain["complete"] is True, (
        "run_phase recorded an incomplete chain for a chain that terminated on a "
        "null token -- the snapshot is being taken before termination is set"
    )
    assert chain["termination"] == "null_next_token"
    assert chain["pages"] == 2


class _HoldoutOnlyClient:
    """A PSKY-shaped symbol: every row falls inside the sealed holdout window."""

    def __init__(self) -> None:
        self.counter = alpaca_data.RequestCounter()

    def iter_bars(self, symbols, timeframe, start, end):
        chain = alpaca_data.PageChain(symbol=symbols[0], timeframe=timeframe)
        chain.pages, chain.last_token = 1, None
        yield (
            {
                "bars": {
                    symbols[0]: [
                        {
                            "t": "2025-06-02T15:00:00Z",
                            "o": 1.0,
                            "h": 2.0,
                            "l": 0.5,
                            "c": 1.5,
                            "v": 9,
                            "n": 1,
                            "vw": 1.2,
                        }
                    ]
                },
                "next_page_token": None,
            },
            chain,
        )
        chain.termination = "null_next_token"


def test_holdout_only_symbol_is_not_counted_as_exploration_coverage(
    artifact_root, monkeypatch
):
    """Source-level fix for the PSKY miscount, not just a corrected artifact.

    `run_phase` used to add a symbol to "has data" on any row, including
    holdout rows, so a holdout-only symbol reported symbols_with_data=1 /
    symbols_empty=0 -- exactly the reading that let PSKY look like ordinary
    exploration coverage.
    """
    monkeypatch.setattr(config, "START_DATE", _dt.date(2025, 1, 1))
    monkeypatch.setattr(config, "CUTOFF_DATE", _dt.date(2025, 12, 31))

    stats = build.run_phase(_HoldoutOnlyClient(), ["PSKY"], "1Min", "1m", 1).as_dict()

    assert stats["rows_exploration"] == 0
    assert stats["rows_holdout"] == 1
    assert stats["symbols_with_data"] == 0, "holdout-only must not count as coverage"
    assert stats["symbols_with_data_any_window"] == 1
    assert stats["symbols_empty"] == 1
    assert stats["zero_exploration_symbols"] == ["PSKY"]


def test_legacy_resume_marker_is_refused_not_silently_reinterpreted(
    artifact_root, monkeypatch
):
    """Legacy markers must fail closed rather than fake a reproduction.

    Markers written before `exploration_symbols` existed hold only the
    any-window list. The previous fallback replayed that as the exploration
    set, so a resumed run reported 500 exploration / 0 empty against a sealed
    manifest saying 499 / PSKY=1. Reproducing the WRONG contract silently is
    worse than refusing to reproduce it.
    """
    monkeypatch.setattr(config, "START_DATE", _dt.date(2024, 1, 1))
    monkeypatch.setattr(config, "CUTOFF_DATE", _dt.date(2024, 12, 31))

    marker = build._marker("1m", 2024, 0)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"rows": 5, "symbols": ["PSKY"]}), encoding="utf-8")

    with pytest.raises(build.LegacyResumeStateError, match="exploration_symbols"):
        build.run_phase(_TwoPageClient(), ["PSKY"], "1Min", "1m", 1)

    # A current-schema marker resumes normally.
    marker.write_text(
        json.dumps({"rows": 5, "symbols": ["PSKY"], "exploration_symbols": []}),
        encoding="utf-8",
    )
    stats = build.run_phase(_TwoPageClient(), ["PSKY"], "1Min", "1m", 1)
    assert stats.units_resumed == 1
    assert stats.symbols_with_data == 0  # exploration set replayed as empty


def test_skip_1m_with_default_phase_is_rejected_not_silently_empty(capsys):
    """Regression: this combination could seal an empty corpus as a success.

    Changing the default phase to 1m left the legacy `--skip-1m` flag
    suppressing the ONLY selected phase, so the run reached finalization with
    exit 0 and an empty phase list. A false-green is the worst failure mode in
    this project, so the combination is refused outright.
    """
    assert build.main(["--skip-1m"]) == 2
    out = capsys.readouterr().out
    assert "selects no phase at all" in out
    assert "empty corpus" in out

    # Explicit 1m with skip is equally empty and equally refused.
    assert build.main(["--phase", "1m", "--skip-1m"]) == 2


def test_scope_c_is_the_default_and_1hour_needs_an_explicit_override(capsys):
    """The declared scope and the default code path must agree.

    config declares SCOPE_DECISION=C_1M_TOP500_ONLY while `--phase` defaulted to
    `both`, so an ordinary re-run would have collected the 1Hour data that the
    manifest records as a deliberate gap.
    """
    assert build.main(["--phase", "1h"]) == 2
    assert "scope is C_1M_TOP500_ONLY" in capsys.readouterr().out


def test_completeness_assessment_is_emitted_from_code(artifact_root):
    """The shipped UNVERIFIED contract must be reproducible from source."""
    blind = [{"complete": False, "termination": "unstarted"}] * 3
    out = build.completeness_assessment(blind)
    assert out["PAGE_COMPLETENESS"] == "UNVERIFIED"
    assert out["completeness_resolution"] == "WEAKENED_CLAIM"
    assert out["metric_status"] == "MIS_INSTRUMENTED_IN_THIS_RUN"
    assert "WITHDRAWN" in out["substitute_arguments_assessed"]["B_stop_then_resume"]

    good = [{"complete": True, "termination": "null_next_token"}] * 3
    assert (
        build.completeness_assessment(good, units_attempted=3)["PAGE_COMPLETENESS"]
        == "VERIFIED"
    )
    # Without a unit count the recorded chains cannot be reconciled against
    # what was attempted, so the claim must not be VERIFIED.
    assert build.completeness_assessment(good)["PAGE_COMPLETENESS"] == "UNVERIFIED"


def test_attempted_unit_with_no_chain_record_forces_unverified():
    """Regression: absent evidence was being read as evidence of completeness.

    A request that raises before its first yield produces a gap but NO chain
    record. Judging only the recorded chains meant the evidence set silently
    shrank to the successes, and `completeness_assessment` returned VERIFIED
    while an attempted chain had no terminal-token evidence at all.
    """
    one_good = [{"complete": True, "termination": "null_next_token"}]

    out = build.completeness_assessment(one_good, units_attempted=2)
    assert out["PAGE_COMPLETENESS"] == "UNVERIFIED"
    assert out["chains_missing"] == 1
    assert any("recorded NO chain" in r for r in out["unverified_because"])

    # Resumed units carry no chain evidence for this run either.
    resumed = build.completeness_assessment(
        one_good, units_attempted=1, units_resumed=4
    )
    assert resumed["PAGE_COMPLETENESS"] == "UNVERIFIED"
    assert any("resumed from markers" in r for r in resumed["unverified_because"])


class _MixedClient:
    """One symbol succeeds; the symbol named BAD raises before its first yield."""

    def __init__(self) -> None:
        self.counter = alpaca_data.RequestCounter()

    def iter_bars(self, symbols, timeframe, start, end):
        if symbols[0] == "BAD":
            raise RuntimeError("connection reset before first page")
        chain = alpaca_data.PageChain(symbol=symbols[0], timeframe=timeframe)
        chain.pages, chain.last_token = 1, None
        yield (
            {
                "bars": {
                    symbols[0]: [
                        {
                            "t": "2024-03-04T15:00:00Z",
                            "o": 1.0,
                            "h": 2.0,
                            "l": 0.5,
                            "c": 1.5,
                            "v": 7,
                            "n": 1,
                            "vw": 1.3,
                        }
                    ]
                },
                "next_page_token": None,
            },
            chain,
        )
        chain.termination = "null_next_token"


def _run_main_e2e(monkeypatch, symbols):
    """Drive build.main() end to end with a fake client and no network."""
    monkeypatch.setattr(config, "START_DATE", _dt.date(2024, 1, 1))
    monkeypatch.setattr(config, "CUTOFF_DATE", _dt.date(2024, 12, 31))
    monkeypatch.setattr(
        build.alpaca_data, "AlpacaDataClient", lambda *a, **k: _MixedClient()
    )
    monkeypatch.setattr(
        build,
        "measure",
        lambda c: {
            "m1_rows_per_request_measured": 9471.0,
            "m1_rows_per_symbol_year_2016": 80000.0,
            "m1_rows_per_symbol_year_2024": 100000.0,
            "requests_already_spent": 0,
        },
    )
    monkeypatch.setattr(build, "load_top500", lambda: symbols)
    rc = build.main([])
    manifest = json.loads(config.MANIFEST_PATH.read_text())
    return rc, manifest


def test_e2e_pre_yield_failure_forces_unverified_in_the_sealed_manifest(
    artifact_root, monkeypatch
):
    """END-TO-END regression -- the unit-level fix alone did not hold.

    R4 fixed `completeness_assessment` in isolation but wired it to
    `units_done`, which only counts successes. A unit that raised before its
    first yield was therefore absent from BOTH the chain list and the
    denominator, so they reconciled and the sealed manifest still published
    PAGE_COMPLETENESS=VERIFIED. The terminal verdict was correctly
    BUILT_WITH_GAPS, but the consumer-facing field was false.

    This drives the real `build.main()` path, because a passing unit test is
    exactly what masked the defect last round.
    """
    _rc, manifest = _run_main_e2e(monkeypatch, ["GOOD", "BAD"])

    phase = manifest["phases"][0]
    assert phase["units_attempted"] == 2, "failures must count toward attempts"
    assert phase["units_done"] == 1, "only the successful unit completed"
    assert phase["page_chains_recorded"] == 1

    assert manifest["page_chain_integrity"]["PAGE_COMPLETENESS"] == "UNVERIFIED"
    assert manifest["terminal_verdict"] == "BUILT_WITH_GAPS"

    report = json.loads((config.REPORTS_DIR / "page_chain_integrity.json").read_text())
    assert report["PAGE_COMPLETENESS"] == "UNVERIFIED"
    assert report["chains_missing"] == 1


def test_e2e_clean_run_still_reaches_verified(artifact_root, monkeypatch):
    """Guard against over-correcting: a fully clean run must stay VERIFIED."""
    _rc, manifest = _run_main_e2e(monkeypatch, ["GOOD", "GOOD2"])

    phase = manifest["phases"][0]
    assert phase["units_attempted"] == phase["units_done"] == 2
    assert manifest["page_chain_integrity"]["PAGE_COMPLETENESS"] == "VERIFIED"
    assert manifest["terminal_verdict"] == "READY_FOR_RESEARCH"


def test_run_phase_that_raises_before_first_yield_leaves_no_chain(artifact_root):
    """Pins the shape of the defect end-to-end, not just the pure function."""

    class _RaisesImmediately:
        def __init__(self):
            self.counter = alpaca_data.RequestCounter()

        def iter_bars(self, symbols, timeframe, start, end):
            raise RuntimeError("connection reset")
            yield  # pragma: no cover

    monkey = build.run_phase(_RaisesImmediately(), ["AAPL"], "1Min", "1m", 1)
    assert monkey.gaps, "a failed unit must be recorded as an explicit gap"
    assert monkey.page_chains == [], "no chain evidence exists for that unit"
    # units_done stays 0 because the unit never completed; the assessment must
    # not read the empty chain list as success.
    assert (
        build.completeness_assessment(monkey.page_chains, units_attempted=1)[
            "PAGE_COMPLETENESS"
        ]
        == "UNVERIFIED"
    )


def test_old_snapshot_form_would_fail_the_regression_guard():
    """Proves the assertion above discriminates, instead of passing vacuously.

    If this ever starts reporting complete=True, the guard above has lost its
    teeth and both tests are worthless.
    """
    old = _old_run_phase_snapshot(_TwoPageClient(), ["AAPL"], "1Min")
    assert old["complete"] is False
    assert old["termination"] == "unstarted"

    live = None
    for _payload, chain in _TwoPageClient().iter_bars(["AAPL"], "1Min", "s", "e"):
        live = chain
    assert live.as_dict()["complete"] is True  # post-exhaustion snapshot


def test_page_chain_marks_incomplete_when_not_exhausted():
    chain = alpaca_data.PageChain(symbol="AAPL", timeframe="1Min")
    chain.pages, chain.termination = 3, "budget"
    assert chain.as_dict()["complete"] is False

    chain.termination = "null_next_token"
    assert chain.as_dict()["complete"] is True


def test_ohlc_violations_detected():
    good = {"symbol": "A", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1}
    bad_order = {**good, "high": 0.1}
    nonpositive = {
        "symbol": "A",
        "open": 0,
        "high": 0,
        "low": 0,
        "close": 0,
        "volume": 1,
    }
    assert bars.ohlc_violations([good]) == []
    assert len(bars.ohlc_violations([bad_order])) == 1
    assert bars.ohlc_violations([nonpositive])[0]["violation"] == "nonpositive_price"


# --------------------------------------------------------------------------
# §3.11 -- top-500 selection must not touch the holdout
# --------------------------------------------------------------------------


def test_selection_report_evidences_holdout_non_contact():
    """Runs against the real shipped snapshot produced by selection.py."""
    report = Path(
        "/Users/mgh3326/work/herdr-artifacts/us-intraday-corpus-v1/inputs/"
        "top500_1m_selection_report.json"
    )
    if not report.exists():
        pytest.skip("selection snapshot not built in this environment")
    data = json.loads(report.read_text())
    evidence = data["holdout_non_contact_evidence"]

    assert evidence["holdout_paths_touched"] == 0
    assert all("holdout" not in p for p in evidence["files_read"])
    assert evidence["max_session_date_in_input"] < str(config.HOLDOUT[0])
    assert data["count"] == config.TOP500_COUNT
    assert data["SURVIVORSHIP_BIASED"] == "TRUE"
