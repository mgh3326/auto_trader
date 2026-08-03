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
    with pytest.raises(AssertionError, match="refusing to read holdout"):
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


def test_chain_snapshot_taken_inside_the_loop_is_premature():
    """Regression: the `complete` flag was always False in the first 1Min run.

    `termination` is only set after the final yield resumes, so a snapshot taken
    inside the consuming loop reads "unstarted" and marks every chain
    incomplete. The fix is to snapshot after the loop is exhausted. This test
    pins both halves of that behaviour so the metric cannot silently go blind
    again -- a page-chain check that always says "incomplete" gives no more
    assurance than one that always says "complete".
    """

    class _FakeClient:
        counter = alpaca_data.RequestCounter()

        def iter_bars(self, symbols, timeframe, start, end):
            chain = alpaca_data.PageChain(symbol=symbols[0], timeframe=timeframe)
            for page in range(2):
                chain.pages += 1
                token = "tok" if page == 0 else None
                chain.last_token = token
                yield {"bars": {}, "next_page_token": token}, chain
                if not token:
                    chain.termination = "null_next_token"
                    return

    client = _FakeClient()

    inside = None
    for _payload, chain in client.iter_bars(["AAPL"], "1Min", "s", "e"):
        inside = chain.as_dict()  # the old, premature snapshot
    assert inside["complete"] is False, "premature snapshot cannot see termination"

    live = None
    for _payload, chain in client.iter_bars(["AAPL"], "1Min", "s", "e"):
        live = chain
    after = live.as_dict()  # the fixed, post-exhaustion snapshot
    assert after["complete"] is True
    assert after["termination"] == "null_next_token"
    assert after["pages"] == 2


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
