"""ROB-941 R1 I1 remediation — build_rob941_corpus.py CLI orchestration.

Previously ``build_corpus()``/``main()`` had ZERO fixture coverage (only the
opt-in live test touched them, always through the real ``af.urllib_opener``).
This file closes that gap with a fully fixture-driven, network-0 exercise of:

- materialize (raw archive + Parquet shard) writes under an injected
  ``artifact_root``, threaded through every one of the 4 frozen symbols;
- the atomic/fail-closed publish gate: ``build_corpus()`` re-verifies its own
  output via ``rob941_offline_loader.load_corpus`` BEFORE returning, and
  ``main()`` never writes/replaces the committed manifest path unless
  ``build_corpus()`` returns successfully (a mid-build failure leaves any
  prior committed manifest untouched -- no partial corpus is ever exposed as
  complete).
"""

import hashlib
import io
import json
import zipfile

import build_rob941_corpus as cli
import canonical_hash
import pytest
import rob941_archive_fetch as af
import rob941_frozen_scope as scope
import rob941_offline_loader as loader
import rob941_persistence as persist
from funding_oi_archive import FundingRow
from rob941_kline_schema import NormalizedKline
from rob941_manifest import (
    CorpusManifest,
    SymbolEligibility,
    SymbolFundingManifest,
    SymbolKlineManifest,
)

HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
FUNDING_HEADER = "calc_time,funding_interval_hours,last_funding_rate\n"


def _kline_row(open_time_ms: int) -> str:
    close_time = open_time_ms + 59_999
    return (
        f"{open_time_ms},100.0,101.0,99.0,100.5,10.0,{close_time},1000.0,5,4.0,400.0,0"
    )


def _zip_and_checksum(name: str, content: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    zb = buf.getvalue()
    checksum = f"{hashlib.sha256(zb).hexdigest()}  {name}\n".encode()
    return zb, checksum


class _FakeFourSymbolUniverse:
    """Tiny fixture opener covering all 4 frozen symbols x 12 months (5 bars
    each) -- enough to exercise build_corpus() end to end without a real
    network call or a slow full-size fixture."""

    def __init__(self, symbols):
        self.table: dict[str, bytes] = {}
        for symbol in symbols:
            self._populate_symbol(symbol)

    def _populate_symbol(self, symbol):
        months = scope.months_in_window()
        for idx, (year, month) in enumerate(months):
            month_start = scope.WINDOW_START_MS + idx * 10 * 60_000
            url = af.kline_archive_url(symbol, "1m", year, month)
            csv_name = f"{symbol}-1m-{year:04d}-{month:02d}.csv"
            lines = [HEADER] + [
                _kline_row(month_start + m * 60_000) + "\n" for m in range(5)
            ]
            zb, chk = _zip_and_checksum(csv_name, "".join(lines))
            self.table[url] = zb
            self.table[url + ".CHECKSUM"] = chk

            fund_url = af.funding_archive_url(symbol, year, month)
            fund_name = f"{symbol}-fundingRate-{year:04d}-{month:02d}.csv"
            fund_text = FUNDING_HEADER + f"{month_start},8,0.0001\n"
            fzb, fchk = _zip_and_checksum(fund_name, fund_text)
            self.table[fund_url] = fzb
            self.table[fund_url + ".CHECKSUM"] = fchk

    def opener(self, url):
        return self.table.get(url)


def test_build_corpus_materializes_and_offline_reverifies_all_four_symbols(tmp_path):
    fake = _FakeFourSymbolUniverse(scope.UNIVERSE)
    manifest = cli.build_corpus(artifact_root=tmp_path, opener=fake.opener)

    assert set(manifest.universe) == set(scope.UNIVERSE)
    for k in manifest.klines:
        assert k.shard_path is not None
        assert (tmp_path / k.shard_path).is_file()
        assert k.shard_file_sha256 is not None
    for f in manifest.funding:
        assert f.shard_path is not None
        assert (tmp_path / f.shard_path).is_file()
    for k in manifest.klines:
        for a in k.archives:
            assert a.local_path is not None, "raw archive must be materialized"
            assert (tmp_path / a.local_path).is_file()

    # build_corpus() must return a manifest that is ALSO independently, offline
    # loadable (the exact gate it ran on itself before returning).
    reloaded = loader.load_corpus(manifest, tmp_path)
    assert set(reloaded["klines"]) == set(scope.UNIVERSE)
    for symbol in scope.UNIVERSE:
        assert len(reloaded["klines"][symbol]) == 60  # 12 months x 5 bars, no gaps


def test_build_corpus_fails_closed_on_checksum_mismatch_without_publishing(tmp_path):
    fake = _FakeFourSymbolUniverse(scope.UNIVERSE)
    any_checksum_key = next(
        k for k in fake.table if k.endswith(".CHECKSUM") and "klines" in k
    )
    fake.table[any_checksum_key] = b"0" * 64 + b"  corrupt.csv\n"
    with pytest.raises(af.ChecksumMismatchError):
        cli.build_corpus(artifact_root=tmp_path, opener=fake.opener)


def _tiny_manifest(artifact_root) -> CorpusManifest:
    """A minimal-but-real, independently-materialized+verifiable manifest, used
    to test main()'s atomic-publish orchestration in isolation from
    build_corpus()'s own fetch/materialize machinery (covered above)."""
    klines = []
    funding = []
    for idx, symbol in enumerate(scope.UNIVERSE):
        rows = [
            NormalizedKline(
                symbol=symbol,
                open_time_ms=scope.WINDOW_START_MS + (idx * 5 + i) * 60_000,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                base_volume=10.0,
                close_time_ms=scope.WINDOW_START_MS + (idx * 5 + i) * 60_000 + 59_999,
                quote_volume=1000.0,
                trade_count=5,
                taker_buy_volume=4.0,
                taker_buy_quote_volume=400.0,
            )
            for i in range(3)
        ]
        rel_path, file_sha = persist.write_kline_shard(artifact_root, symbol, rows)
        klines.append(
            SymbolKlineManifest(
                symbol=symbol,
                interval="1m",
                archives=(),
                normalized_shard_sha256=canonical_hash.canonical_sha256(
                    [r.__dict__ for r in rows]
                ),
                shard_path=rel_path,
                shard_file_sha256=file_sha,
                row_count=len(rows),
                min_open_time_ms=rows[0].open_time_ms,
                max_open_time_ms=rows[-1].open_time_ms,
                gap_ranges=(),
            )
        )
        frows = [
            FundingRow(
                calc_time=scope.WINDOW_START_MS + idx * 3_600_000,
                funding_interval_hours=8,
                last_funding_rate=0.0001,
            )
        ]
        frel, ffile_sha = persist.write_funding_shard(artifact_root, symbol, frows)
        funding.append(
            SymbolFundingManifest(
                symbol=symbol,
                archives=(),
                normalized_shard_sha256=canonical_hash.canonical_sha256(
                    [r.__dict__ for r in frows]
                ),
                shard_path=frel,
                shard_file_sha256=ffile_sha,
                row_count=1,
                min_calc_time_ms=frows[0].calc_time,
                max_calc_time_ms=frows[0].calc_time,
            )
        )
    eligibility = tuple(
        SymbolEligibility(symbol=s, **scope.eligibility(s)) for s in scope.UNIVERSE
    )
    return CorpusManifest(
        window_start_iso=scope.WINDOW_START_ISO,
        window_end_iso=scope.WINDOW_END_ISO,
        universe=scope.UNIVERSE,
        eligibility=eligibility,
        klines=tuple(klines),
        funding=tuple(funding),
    )


def test_main_atomic_publish_writes_committed_manifest_only_on_success(
    tmp_path, monkeypatch
):
    artifact_root = tmp_path / "artifact_root"
    artifact_root.mkdir()
    fixed_manifest = _tiny_manifest(artifact_root)
    out_path = tmp_path / "data_manifests" / "rob941_corpus_manifest.v1.json"

    monkeypatch.setattr(cli, "build_corpus", lambda artifact_root: fixed_manifest)
    monkeypatch.setattr("artifact_paths.pit_data_root", lambda: artifact_root)

    rc = cli.main(["--run", "--out", str(out_path)])
    assert rc == 0
    assert out_path.is_file()
    saved = CorpusManifest.load(out_path)
    assert saved.content_hash() == fixed_manifest.content_hash()
    assert not out_path.with_suffix(out_path.suffix + ".tmp").exists()


def test_main_never_publishes_when_build_corpus_raises(tmp_path, monkeypatch):
    out_path = tmp_path / "data_manifests" / "rob941_corpus_manifest.v1.json"

    def _boom(artifact_root):
        raise af.ChecksumMismatchError("simulated mid-build failure")

    monkeypatch.setattr(cli, "build_corpus", _boom)
    monkeypatch.setattr(
        "artifact_paths.pit_data_root", lambda: tmp_path / "artifact_root"
    )

    with pytest.raises(af.ChecksumMismatchError):
        cli.main(["--run", "--out", str(out_path)])
    assert not out_path.exists()


def test_main_atomic_publish_never_overwrites_prior_committed_manifest_on_failure(
    tmp_path, monkeypatch
):
    out_path = tmp_path / "data_manifests" / "rob941_corpus_manifest.v1.json"
    out_path.parent.mkdir(parents=True)
    out_path.write_text('{"prior": "committed manifest"}')

    def _boom(artifact_root):
        raise af.ChecksumMismatchError("simulated mid-build failure")

    monkeypatch.setattr(cli, "build_corpus", _boom)
    monkeypatch.setattr(
        "artifact_paths.pit_data_root", lambda: tmp_path / "artifact_root"
    )

    with pytest.raises(af.ChecksumMismatchError):
        cli.main(["--run", "--out", str(out_path)])
    assert json.loads(out_path.read_text()) == {"prior": "committed manifest"}


def test_main_no_run_flag_is_still_a_no_op(capsys):
    rc = cli.main([])
    assert rc == 0
    assert "no-op" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# captain review (ultrathink atomicity correction): a later build that fails
# partway must NEVER mutate the bytes a prior, still-published manifest
# references. Content-addressed paths (rob941_persistence: checksum/row-hash
# embedded in every path) are the mechanism -- a rebuild only ever "rewrites"
# an existing path when the content is byte-identical (a no-op skip), and
# genuinely different content always lands at a genuinely different path. This
# asserts the guarantee end to end, not just that the mechanism exists.
# --------------------------------------------------------------------------- #
def _snapshot_referenced_bytes(manifest: CorpusManifest, artifact_root):
    paths = set()
    for k in manifest.klines:
        paths.add(k.shard_path)
        paths.update(a.local_path for a in k.archives)
    for f in manifest.funding:
        paths.add(f.shard_path)
        paths.update(a.local_path for a in f.archives)
    assert all(p is not None for p in paths), (
        "every referenced path must be materialized"
    )
    return {p: (artifact_root / p).read_bytes() for p in paths}


def test_prior_published_corpus_survives_a_failed_rebuild_byte_for_byte(tmp_path):
    artifact_root = tmp_path / "artifact_root"
    out_path = tmp_path / "data_manifests" / "rob941_corpus_manifest.v1.json"

    # ONE shared fixture object for both attempts: BTCUSDT (processed first in
    # frozen.UNIVERSE order) is untouched, so re-running against the SAME bytes
    # forces a genuine same-content-addressed-path "rewrite" attempt for its 12
    # archives + shard during the second (failing) build -- not merely two
    # unrelated builds that happen to never collide on any path.
    fake = _FakeFourSymbolUniverse(scope.UNIVERSE)
    manifest_v1 = cli.build_corpus(artifact_root=artifact_root, opener=fake.opener)
    cli._atomic_save(manifest_v1, out_path)
    before = _snapshot_referenced_bytes(manifest_v1, artifact_root)

    # corrupt XRPUSDT's checksum (2nd symbol in frozen.UNIVERSE) so BTCUSDT
    # fully re-completes (real same-path collision) before the build fails.
    xrp_checksum_key = next(
        k
        for k in fake.table
        if k.endswith(".CHECKSUM") and "klines" in k and "XRPUSDT" in k
    )
    original = fake.table[xrp_checksum_key]
    fake.table[xrp_checksum_key] = b"0" * 64 + b"  corrupt.csv\n"
    try:
        with pytest.raises(af.ChecksumMismatchError):
            cli.build_corpus(artifact_root=artifact_root, opener=fake.opener)
    finally:
        fake.table[xrp_checksum_key] = original

    # every byte the v1 manifest references is untouched, including BTCUSDT's
    # content-addressed shard/archives that the failed rebuild re-wrote to the
    # exact same paths (a safe, verified-identical no-op, not a corruption)
    after = _snapshot_referenced_bytes(manifest_v1, artifact_root)
    assert after == before

    # the committed manifest file was never touched by the failed rebuild,
    # and it is still fully, independently offline-loadable
    committed = CorpusManifest.load(out_path)
    assert committed.content_hash() == manifest_v1.content_hash()
    reloaded = loader.load_corpus(committed, artifact_root)
    assert set(reloaded["klines"]) == set(scope.UNIVERSE)


# --------------------------------------------------------------------------- #
# captain review (ultrathink determinism follow-up): an identical rerun (same
# upstream/fixture bytes) must reproduce the exact same manifest content_hash
# and the exact same shard_path/local_path values -- no random/time-based
# generation id anywhere in the path scheme.
# --------------------------------------------------------------------------- #
def test_identical_rerun_reproduces_identical_manifest_hash_and_paths(tmp_path):
    fake = _FakeFourSymbolUniverse(scope.UNIVERSE)  # ONE fixture object, reused as-is
    root1 = tmp_path / "root1"
    root2 = tmp_path / "root2"

    manifest1 = cli.build_corpus(artifact_root=root1, opener=fake.opener)
    manifest2 = cli.build_corpus(artifact_root=root2, opener=fake.opener)

    assert manifest1.content_hash() == manifest2.content_hash()
    for k1, k2 in zip(manifest1.klines, manifest2.klines, strict=True):
        assert k1.shard_path == k2.shard_path
        assert k1.shard_file_sha256 == k2.shard_file_sha256
        for a1, a2 in zip(k1.archives, k2.archives, strict=True):
            assert a1.local_path == a2.local_path
            assert a1.checksum_sha256 == a2.checksum_sha256
    for f1, f2 in zip(manifest1.funding, manifest2.funding, strict=True):
        assert f1.shard_path == f2.shard_path
        assert f1.shard_file_sha256 == f2.shard_file_sha256


def test_rerunning_build_corpus_against_the_same_artifact_root_is_idempotent(tmp_path):
    fake = _FakeFourSymbolUniverse(scope.UNIVERSE)
    artifact_root = tmp_path / "artifact_root"

    manifest1 = cli.build_corpus(artifact_root=artifact_root, opener=fake.opener)
    before = _snapshot_referenced_bytes(manifest1, artifact_root)
    manifest2 = cli.build_corpus(artifact_root=artifact_root, opener=fake.opener)
    after = _snapshot_referenced_bytes(manifest2, artifact_root)

    assert manifest1.content_hash() == manifest2.content_hash()
    assert before == after
