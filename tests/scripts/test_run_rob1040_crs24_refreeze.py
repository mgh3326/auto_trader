"""ROB-1040 CRS-24 CORR-1 real-corpus refreeze launcher.

Every gate rejection path is exercised with fakes/tmp_path fixtures -- this
suite never reads, loads, or summarizes anything under the real frozen
ROB-941 corpus root (``~/work/herdr-artifacts/rob941-4bcc2da9.../``). The one
end-to-end smoke test that exercises the real-corpus loading pipeline
(``_load_real_corpus_binding``) uses a hand-built, tiny, entirely synthetic
in-memory kline dict -- never the real corpus.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import run_rob1040_crs24_refreeze as launcher

_SCRIPT = Path(launcher.__file__).resolve()
_LAUNCHER_SHA256 = hashlib.sha256(_SCRIPT.read_bytes()).hexdigest()

# Several tests import CRS-24 research modules directly (to build fixtures,
# e.g. the exact expected reference-key domain); the launcher only adds
# `research/nautilus_scalping` to `sys.path` lazily inside its own gated
# functions, so tests need the same idempotent installation up front.
launcher._install_runtime_paths()


def _stdio() -> tuple[io.StringIO, io.StringIO]:
    return io.StringIO(), io.StringIO()


# ---------------------------------------------------------------------------
# Dry-run / usage
# ---------------------------------------------------------------------------


def test_no_arguments_are_dry_run_only_and_effectless() -> None:
    stdout, stderr = _stdio()
    assert launcher.run_cli((), stdout=stdout, stderr=stderr) == 0
    payload = json.loads(stdout.getvalue())
    assert payload["default_state"] == "DISABLED"
    assert payload["run_requested"] is False
    assert all(value in (0, False) for value in payload["effects"].values())
    assert payload["refreeze_head"] == launcher.CRS24_MERGE_REFREEZE_HEAD
    assert payload["seven_file_seal"]["changed_files"] == ["rob1040_crs24_evidence.py"]
    assert stderr.getvalue() == ""


def test_no_mode_flag_is_a_usage_error() -> None:
    stdout, stderr = _stdio()
    assert (
        launcher.run_cli(
            ("--manifest", "x", "--corpus-root", "y"), stdout=stdout, stderr=stderr
        )
        == launcher.CLI_USAGE_OR_PLAN_ERROR
    )
    assert stdout.getvalue() == ""


def test_preflight_missing_required_argument_is_a_usage_error() -> None:
    stdout, stderr = _stdio()
    assert (
        launcher.run_cli(("--preflight",), stdout=stdout, stderr=stderr)
        == launcher.CLI_USAGE_OR_PLAN_ERROR
    )


def test_run_one_shot_without_confirm_phrase_is_a_usage_error() -> None:
    stdout, stderr = _stdio()
    assert (
        launcher.run_cli(
            (
                "--run-one-shot",
                "--manifest",
                "x",
                "--corpus-root",
                "y",
                "--output-root",
                "z",
                "--launcher-sha256",
                _LAUNCHER_SHA256,
            ),
            stdout=stdout,
            stderr=stderr,
        )
        == launcher.CLI_USAGE_OR_PLAN_ERROR
    )


# ---------------------------------------------------------------------------
# Individual gate functions
# ---------------------------------------------------------------------------


def test_launcher_self_sha256_gate() -> None:
    launcher._require_launcher_self_sha256(_LAUNCHER_SHA256)
    with pytest.raises(
        launcher.LaunchRefused, match="LAUNCHER_PHYSICAL_SHA256_MISMATCH"
    ):
        launcher._require_launcher_self_sha256("0" * 64)


def test_refreeze_head_ancestor_gate_passes_on_this_worktree() -> None:
    # The worktree this test runs in was branched from a commit descending
    # from the CRS-24 merge commit, so this must pass without a monkeypatch.
    launcher._require_refreeze_head_ancestor()


def test_refreeze_head_ancestor_gate_rejects_an_unknown_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "CRS24_MERGE_REFREEZE_HEAD", "f" * 40)
    with pytest.raises(launcher.LaunchRefused, match="REFREEZE_HEAD_NOT_ANCESTOR"):
        launcher._require_refreeze_head_ancestor()


def test_clean_worktree_gate_reflects_actual_git_status() -> None:
    # This branch has new untracked/modified files at test time (the PR this
    # suite ships in), so the real worktree is NOT clean -- assert the gate
    # actually observes that rather than asserting a fixed outcome.
    dirty = launcher._git("status", "--porcelain", "--untracked-files=all")
    if dirty:
        with pytest.raises(launcher.LaunchRefused, match="WORKTREE_NOT_CLEAN"):
            launcher._require_clean_worktree()
    else:
        launcher._require_clean_worktree()


def test_seven_file_seal_gate_passes_against_the_real_worktree() -> None:
    launcher._require_seven_file_seal(launcher.CRS24_CURRENT_REFREEZE_FILE_DIGESTS)


def test_seven_file_seal_gate_rejects_a_tampered_expectation() -> None:
    tampered = dict(launcher.CRS24_CURRENT_REFREEZE_FILE_DIGESTS)
    tampered["rob1040_crs24_contracts.py"] = "0" * 64
    with pytest.raises(
        launcher.LaunchRefused,
        match="SEVEN_FILE_DIGEST_MISMATCH:rob1040_crs24_contracts.py",
    ):
        launcher._require_seven_file_seal(tampered)


def test_seven_file_seal_gate_rejects_a_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "RESEARCH_ROOT", tmp_path)
    with pytest.raises(launcher.LaunchRefused, match="SEALED_FILE_MISSING_OR_UNSAFE"):
        launcher._require_seven_file_seal(launcher.CRS24_CURRENT_REFREEZE_FILE_DIGESTS)


def test_require_paths_accepts_the_exact_expected_triple(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}")
    corpus_root_path = tmp_path / "corpus"
    corpus_root_path.mkdir()
    output_root_path = tmp_path / "out"
    monkeypatch.setattr(launcher, "EXPECTED_MANIFEST", manifest_path)
    monkeypatch.setattr(launcher, "EXPECTED_CORPUS_ROOT", corpus_root_path)
    monkeypatch.setattr(launcher, "EXPECTED_OUTPUT_ROOT", output_root_path)

    class _Args:
        manifest = str(manifest_path)
        corpus_root = str(corpus_root_path)
        output_root = str(output_root_path)

    resolved_manifest, resolved_corpus, resolved_output = launcher._require_paths(
        _Args()
    )
    assert resolved_manifest == manifest_path.resolve()
    assert resolved_corpus == corpus_root_path.resolve()
    assert resolved_output == output_root_path


@pytest.mark.parametrize(
    "field,reason",
    (
        ("manifest", "MANIFEST_PATH_MISMATCH"),
        ("corpus_root", "CORPUS_ROOT_PATH_MISMATCH"),
        ("output_root", "OUTPUT_ROOT_PATH_MISMATCH"),
    ),
)
def test_require_paths_rejects_a_mismatched_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, reason: str
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    output_root = tmp_path / "out"
    monkeypatch.setattr(launcher, "EXPECTED_MANIFEST", manifest)
    monkeypatch.setattr(launcher, "EXPECTED_CORPUS_ROOT", corpus_root)
    monkeypatch.setattr(launcher, "EXPECTED_OUTPUT_ROOT", output_root)

    values = {
        "manifest": str(manifest),
        "corpus_root": str(corpus_root),
        "output_root": str(output_root),
    }
    other = tmp_path / "other"
    if field == "output_root":
        values[field] = str(other)
    else:
        other.mkdir()
        values[field] = str(other)

    class _Args:
        manifest = values["manifest"]
        corpus_root = values["corpus_root"]
        output_root = values["output_root"]

    with pytest.raises(launcher.LaunchRefused, match=reason):
        launcher._require_paths(_Args())


def test_one_shot_marker_gate(tmp_path: Path) -> None:
    launcher._require_one_shot_not_consumed(tmp_path)
    (tmp_path / launcher.ONE_SHOT_MARKER_NAME).write_text("{}")
    with pytest.raises(launcher.LaunchRefused, match="ONE_SHOT_ALREADY_CONSUMED"):
        launcher._require_one_shot_not_consumed(tmp_path)


# ---------------------------------------------------------------------------
# PIT lookback coverage (metadata-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeKlineManifestRow:
    symbol: str
    min_open_time_ms: int


@dataclass(frozen=True)
class _FakeManifest:
    klines: tuple[_FakeKlineManifestRow, ...]

    def content_hash(self) -> str:
        return "2" * 64


def test_pit_lookback_coverage_gate_passes_with_ample_history() -> None:
    from rob974_h4_contracts import exact_h4_folds
    from rob1040_crs24_feasibility import scheduled_cutoffs

    first_cutoff = scheduled_cutoffs(exact_h4_folds()[0])[0]
    manifest = _FakeManifest(
        tuple(
            _FakeKlineManifestRow(symbol, first_cutoff - 200 * 86_400_000)
            for symbol in launcher.SELECTED_SYMBOLS
        )
    )
    launcher._require_pit_lookback_coverage(manifest)


def test_pit_lookback_coverage_gate_refuses_insufficient_history() -> None:
    from rob974_h4_contracts import exact_h4_folds
    from rob1040_crs24_feasibility import scheduled_cutoffs

    first_cutoff = scheduled_cutoffs(exact_h4_folds()[0])[0]
    manifest = _FakeManifest(
        (
            _FakeKlineManifestRow("XRPUSDT", first_cutoff - 200 * 86_400_000),
            _FakeKlineManifestRow("DOGEUSDT", first_cutoff),  # far too little history
            _FakeKlineManifestRow("SOLUSDT", first_cutoff - 200 * 86_400_000),
        )
    )
    with pytest.raises(
        launcher.LaunchRefused, match="PIT_LOOKBACK_COVERAGE_INSUFFICIENT"
    ):
        launcher._require_pit_lookback_coverage(manifest)


# ---------------------------------------------------------------------------
# Reference-surface construction (pure, no corpus loader involved)
# ---------------------------------------------------------------------------


def test_build_reference_surface_from_a_tiny_hand_built_minute_series() -> None:
    from decimal import Decimal

    from rob974_features import MinuteBar
    from rob1040_crs24_feasibility import expected_entry_reference_keys

    first_entry_key = expected_entry_reference_keys()[0]
    bar = MinuteBar(first_entry_key.timestamp_ms, 1.25, 1.26, 1.24, 1.255, 10.0)
    minute_rows = {
        "XRPUSDT": (bar,) if first_entry_key.symbol == "XRPUSDT" else (),
        "DOGEUSDT": (bar,) if first_entry_key.symbol == "DOGEUSDT" else (),
        "SOLUSDT": (bar,) if first_entry_key.symbol == "SOLUSDT" else (),
    }
    surface = launcher._build_reference_surface(minute_rows)
    matched = surface.entry_observation(first_entry_key)
    assert matched.value == Decimal(str(bar.open))
    # every other key in the domain is untouched -> None/absent sentinel
    other_entry = next(
        key for key in expected_entry_reference_keys() if key != first_entry_key
    )
    assert surface.entry_observation(other_entry).value is None


# ---------------------------------------------------------------------------
# Small fake corpus, full launcher-wiring smoke (never touches real corpus)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeCorpusRow:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    base_volume: float


def test_load_real_corpus_binding_with_a_tiny_fake_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the launcher's own corpus->binding wiring end-to-end against
    a hand-built, 1-row-per-symbol fake kline dict -- proves MinuteBar
    construction, 4h aggregation, generator/reference-surface wiring, and
    ``CampaignInputBinding.for_real_corpus`` all connect correctly, without
    reading a single byte from the real ROB-941 corpus."""
    launcher._install_runtime_paths()
    import rob941_offline_loader
    from rob1040_crs24_feasibility import expected_entry_reference_keys

    first_entry_key = expected_entry_reference_keys()[0]
    fake_row = _FakeCorpusRow(first_entry_key.timestamp_ms, 2.0, 2.1, 1.9, 2.05, 5.0)
    fake_loaded = {
        "klines": {
            symbol: [fake_row] if symbol == first_entry_key.symbol else []
            for symbol in launcher.SELECTED_SYMBOLS
        },
        "funding": {symbol: [] for symbol in launcher.SELECTED_SYMBOLS},
    }
    monkeypatch.setattr(
        rob941_offline_loader,
        "load_corpus",
        lambda manifest, corpus_root: fake_loaded,
    )
    manifest = _FakeManifest(())
    binding = launcher._load_real_corpus_binding(manifest, Path("/does/not/matter"))
    assert binding.posture == "refrozen_real_corpus"
    assert binding.extra_authority == (
        ("corpus_manifest_content_sha256", manifest.content_hash()),
    )


def test_load_real_corpus_binding_refuses_a_missing_selected_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher._install_runtime_paths()
    import rob941_offline_loader

    fake_loaded = {
        "klines": {"XRPUSDT": [], "DOGEUSDT": []},  # SOLUSDT missing
        "funding": {},
    }
    monkeypatch.setattr(
        rob941_offline_loader,
        "load_corpus",
        lambda manifest, corpus_root: fake_loaded,
    )
    manifest = _FakeManifest(())
    with pytest.raises(launcher.LaunchRefused, match="SELECTED_CORPUS_SYMBOL_MISSING"):
        launcher._load_real_corpus_binding(manifest, Path("/does/not/matter"))


# ---------------------------------------------------------------------------
# Full CLI wiring: preflight computes no counts; run-one-shot writes once
# ---------------------------------------------------------------------------


def _empty_fake_binding(version: str = "rob1040.crs24.corr1.real_corpus.v1"):
    from rob1040_crs24_evidence import CampaignInputBinding
    from rob1040_crs24_feasibility import (
        EntryReference,
        ExitPresence,
        ReferenceSurface,
        expected_entry_reference_keys,
        expected_exit_presence_keys,
    )
    from rob1040_crs24_features import CRSFeatureGenerator

    generator = CRSFeatureGenerator({"XRPUSDT": (), "DOGEUSDT": (), "SOLUSDT": ()})
    entries = tuple(
        EntryReference(key, None) for key in expected_entry_reference_keys()
    )
    exit_presence = tuple(
        ExitPresence(key, False) for key in expected_exit_presence_keys()
    )
    references = ReferenceSurface(entries, exit_presence)
    return CampaignInputBinding.for_real_corpus(
        version=version,
        generator=generator,
        references=references,
        corpus_manifest_content_sha256="3" * 64,
    )


def _wire_common_fakes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path, Path]:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    output_root = tmp_path / "out"
    monkeypatch.setattr(launcher, "EXPECTED_MANIFEST", manifest)
    monkeypatch.setattr(launcher, "EXPECTED_CORPUS_ROOT", corpus_root)
    monkeypatch.setattr(launcher, "EXPECTED_OUTPUT_ROOT", output_root)
    monkeypatch.setattr(launcher, "_verify_manifest", lambda path: _FakeManifest(()))
    monkeypatch.setattr(
        launcher, "_require_pit_lookback_coverage", lambda manifest: None
    )
    monkeypatch.setattr(
        launcher,
        "_load_real_corpus_binding",
        lambda manifest, root: _empty_fake_binding(),
    )
    return manifest, corpus_root, output_root


def test_preflight_never_computes_or_prints_any_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, corpus_root, output_root = _wire_common_fakes(monkeypatch, tmp_path)
    stdout, stderr = _stdio()
    exit_code = launcher.run_cli(
        (
            "--preflight",
            "--manifest",
            str(manifest),
            "--corpus-root",
            str(corpus_root),
            "--output-root",
            str(output_root),
            "--launcher-sha256",
            _LAUNCHER_SHA256,
        ),
        stdout=stdout,
        stderr=stderr,
    )
    assert exit_code == 0, stderr.getvalue()
    payload = json.loads(stdout.getvalue())
    assert payload["mode"] == "preflight"
    assert payload["counts_computed"] is False
    assert "planned" not in json.dumps(payload)
    assert not output_root.exists()

    # Idempotent: running it again does not fail and still writes nothing.
    stdout2, stderr2 = _stdio()
    assert (
        launcher.run_cli(
            (
                "--preflight",
                "--manifest",
                str(manifest),
                "--corpus-root",
                str(corpus_root),
                "--output-root",
                str(output_root),
                "--launcher-sha256",
                _LAUNCHER_SHA256,
            ),
            stdout=stdout2,
            stderr=stderr2,
        )
        == 0
    )
    assert not output_root.exists()


def test_run_one_shot_writes_artifacts_and_marker_then_refuses_a_second_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, corpus_root, output_root = _wire_common_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, "_require_clean_worktree", lambda: None)
    argv = (
        "--run-one-shot",
        "--manifest",
        str(manifest),
        "--corpus-root",
        str(corpus_root),
        "--output-root",
        str(output_root),
        "--launcher-sha256",
        _LAUNCHER_SHA256,
        "--confirm-one-shot-oos-dry-count",
        launcher.ONE_SHOT_CONFIRMATION,
    )
    stdout, stderr = _stdio()
    exit_code = launcher.run_cli(argv, stdout=stdout, stderr=stderr)
    assert exit_code == 0, stderr.getvalue()
    result = json.loads(stdout.getvalue())
    assert result["mode"] == "run_one_shot"
    assert (output_root / launcher.EVIDENCE_ARTIFACT_NAME).is_file()
    assert (output_root / launcher.METADATA_ARTIFACT_NAME).is_file()
    assert (output_root / launcher.ONE_SHOT_MARKER_NAME).is_file()
    evidence_payload = json.loads(
        (output_root / launcher.EVIDENCE_ARTIFACT_NAME).read_text()
    )
    assert evidence_payload["authorities"]["input"]["posture"] == "refrozen_real_corpus"
    assert evidence_payload["campaign"]["planned"] == 0

    stdout2, stderr2 = _stdio()
    second_exit_code = launcher.run_cli(argv, stdout=stdout2, stderr=stderr2)
    assert second_exit_code == launcher.LAUNCH_REFUSED
    assert "ONE_SHOT_ALREADY_CONSUMED" in stderr2.getvalue()


def test_run_one_shot_rejects_a_wrong_confirmation_phrase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, corpus_root, output_root = _wire_common_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, "_require_clean_worktree", lambda: None)
    stdout, stderr = _stdio()
    exit_code = launcher.run_cli(
        (
            "--run-one-shot",
            "--manifest",
            str(manifest),
            "--corpus-root",
            str(corpus_root),
            "--output-root",
            str(output_root),
            "--launcher-sha256",
            _LAUNCHER_SHA256,
            "--confirm-one-shot-oos-dry-count",
            "not the phrase",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    assert exit_code == launcher.LAUNCH_REFUSED
    assert "CONFIRM_PHRASE_MISMATCH" in stderr.getvalue()
    assert not output_root.exists()
