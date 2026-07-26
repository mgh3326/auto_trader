#!/usr/bin/env python3
"""Default-disabled ROB-1040 CRS-24 CORR-1 real-corpus refreeze launcher.

With no arguments this command performs no corpus load, no file read beyond
its own source, and no artifact write — it only prints its dry-run contract.
Two gated modes exist beyond that:

``--preflight``
    Verifies every static gate (refreeze head ancestry, the seven-file CRS-24
    code seal, the frozen corpus/manifest pins, and PIT lookback coverage
    from manifest metadata only), loads the real corpus fully offline, and
    builds the ``CampaignInputBinding`` + validated campaign context — but
    stops BEFORE any feature/gate/candidate/occupancy evaluation, so it never
    computes or prints an OOS incidence count. It is idempotent and may be
    re-run any number of times; it never writes the one-shot marker.

``--run-one-shot``
    Everything ``--preflight`` does, plus three gates preflight is exempt
    from — ``HEAD == origin/main`` (Linear ROB-1040 실행순서 step 3: the seal
    must be taken on the *merged exact main tree*, not merely on a descendant
    of the refreeze head), a clean worktree, and the exact literal one-shot
    confirmation phrase — plus the full CRS-24 campaign evaluation via the
    sealed ``rob1040_crs24_evidence`` decision closure (unmodified by this
    launcher — this file only supplies the input binding), plus writing the
    evidence/metadata artifacts and a one-shot exhaustion marker. The marker
    is written ``ARMED`` *before* the evaluation and promoted to ``CONSUMED``
    only after every artifact write, so an interrupted run cannot silently
    re-arm. A second invocation against the same ``--output-root`` refuses
    fail-closed in either state.

This launcher performs no PnL/forward-return computation; the sealed CRS-24
decision logic already accepts no realized-outcome columns and emits none
(exit is timestamp-presence-only). This file only builds the two inputs the
sealed module accepts as a caller-supplied binding: a ``CRSFeatureGenerator``
built from real 1-minute corpus bars, and a ``ReferenceSurface`` built from
the same real 1-minute bars' open (entry) / presence (exit).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal
from io import TextIOBase
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_ROOT = REPO_ROOT / "research" / "nautilus_scalping"

# ---------------------------------------------------------------------------
# Stage-3 code-seal procedure (preregistration.md): the merge commit SHA on
# main that carries the CRS-24 implementation, plus a `shasum -a 256` table
# for each of the seven CRS-24 implementation files AT THAT MERGE COMMIT.
# This launcher's own PR changes exactly one of the seven files
# (`rob1040_crs24_evidence.py`, to open one caller-supplied input-binding
# seam; the decision logic body is untouched) — the "current" table below
# records that single expected change explicitly rather than silently
# re-pinning it, so a reviewer can diff the two tables and see exactly one
# entry differ.
# ---------------------------------------------------------------------------
CRS24_MERGE_REFREEZE_HEAD = "0534db071b7b3820f7e0681f66da408a8ae0cc35"

CRS24_FILE_DIGESTS_AT_MERGE_COMMIT: dict[str, str] = {
    "rob1040_crs24_contracts.py": (
        "1f26ebec4809f3fa45705e4f9115087eeda762c7055a8f2381128874e016b15c"
    ),
    "rob1040_crs24_features.py": (
        "9fa529b878f4b454ee1968d65909ff7b8b43a04daf0de87cf060642175688684"
    ),
    "rob1040_crs24_feasibility.py": (
        "618a5775653f298c055f9785687ab14ed4ba572ab5c8403697d509d509cb31ba"
    ),
    "rob1040_crs24_evidence.py": (
        "e5549b146268bc48cc3f190529cd5a51e6874e863f58858af2a6cf9f01084ae8"
    ),
    "rob1040_crs24_synthetic.py": (
        "ccc5bf16e67189d2c824996a934326ef469ad4c2ddbe5af6bb9cf78c30fa8500"
    ),
    "rob1040_crs24_cli.py": (
        "7169799f2f04532c914804d129f5e02faa776818152aa3c047f5bdd4a779a806"
    ),
    "run_rob1040_crs24.py": (
        "f994aecd3537cae3a9653b06c6793007f511ad16151d777dd807655e84765e62"
    ),
}

# Expected digests IN THIS WORKTREE (post-refreeze-launcher-PR). Six entries
# are byte-identical to the merge-commit table; only `rob1040_crs24_evidence.py`
# differs (the one intentional, minimal, behavior-neutral-for-synthetic seam
# opening described in the PR). `CRS24_CHANGED_FILES` names exactly which
# entries are expected to differ, so the launcher can assert both "the six
# untouched files are byte-identical to the merge commit" and "the changed
# file is byte-identical to what THIS PR actually shipped" without silently
# hiding the diff.
CRS24_CHANGED_FILES: frozenset[str] = frozenset({"rob1040_crs24_evidence.py"})

CRS24_CURRENT_REFREEZE_FILE_DIGESTS: dict[str, str] = {
    **CRS24_FILE_DIGESTS_AT_MERGE_COMMIT,
    "rob1040_crs24_evidence.py": (
        "93bbee16960858cb79beabf821cd9a0d04835cd555066b2ca6ed82473c0916ea"
    ),
}

# ---------------------------------------------------------------------------
# Defence-in-depth beyond the preregistration's seven-file table: the CRS-24
# fold/calendar authority modules the campaign REUSES (Linear "구현 경계":
# `rob944_folds.generate_frozen_fold_schedule` /
# `rob974_h4_contracts.exact_h4_folds`). They are outside the sealed seven, so
# the seven-file table alone would let a drifted calendar authority through
# while the *stale* fold-schedule digest still got stamped into all 24 cells.
# `_require_contract_authority` re-verifies these digests AND calls
# `rob1040_crs24_contracts.validate_contract()`, which recomputes the fold /
# filter / contract digests from the live authorities at run time. Kept in a
# separate table from the seven-file seal so a reviewer never confuses the
# preregistered seal with this additional guard.
# ---------------------------------------------------------------------------
CRS24_AUTHORITY_FILE_DIGESTS: dict[str, str] = {
    "rob944_folds.py": (
        "97a646b96647aca40f953701e04aa2d081fcf5b9bc50a654981a01feed40add0"
    ),
    "rob974_h4_contracts.py": (
        "db68351c04321fa8e92054af930ebe78d1082a7c9f0862a1d9e7eb72b1d3499a"
    ),
}

# ---------------------------------------------------------------------------
# Frozen corpus/manifest identity. This is the SAME rob941 corpus and the
# SAME committed manifest file `scripts/run_rob974_r2_campaign.py` already
# pins (`PARENT_CONTENT_SHA256`/`PARENT_MANIFEST_SHA256`) — CRS-24 shares the
# exact same frozen window/universe authority (`rob974_h4_contracts`). The
# manifest JSON itself (row counts, archive checksums, shard paths) is
# committed, non-sensitive provenance metadata; the corpus PARQUET DATA under
# `EXPECTED_CORPUS_ROOT` is what must never be read/loaded/summarized outside
# a `--run-one-shot`/`--preflight` invocation of this launcher.
# ---------------------------------------------------------------------------
EXPECTED_MANIFEST = RESEARCH_ROOT / "data_manifests" / "rob941_corpus_manifest.v1.json"
EXPECTED_CORPUS_ROOT = Path(
    "/Users/mgh3326/work/herdr-artifacts/"
    "rob941-4bcc2da979b47caa45b5f90a09c326aefff91fa605e110d55ef316d53c9a9351/"
    "data"
)
EXPECTED_PARENT_CONTENT_SHA256 = (
    "4bcc2da979b47caa45b5f90a09c326aefff91fa605e110d55ef316d53c9a9351"
)
EXPECTED_PARENT_MANIFEST_SHA256 = (
    "0767b44f976bf717cdc26bbcb0d01da1800418668f9f153461ce62486de10721"
)

# Dedicated one-shot output root for THIS campaign (distinct from the
# ROB-974 R2 output roots). Not previously assigned by an orch packet for
# ROB-1040 -- picked here following the same `herdr-artifacts/<slug>` naming
# convention as `scripts/run_rob974_r2_campaign.py`; flagged in the PR/report
# as a point an approver may want to override before the real one-shot run.
EXPECTED_OUTPUT_ROOT = Path(
    "/Users/mgh3326/work/herdr-artifacts/rob1040-crs24-corr1-refreeze-v1"
)

SELECTED_SYMBOLS = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
REAL_CORPUS_BINDING_VERSION = "rob1040.crs24.corr1.real_corpus.v1"
# Single durable one-shot marker file with an explicit lifecycle state.
# `ARMED` is written (exclusive-create) BEFORE the sealed evaluation begins, so
# a crash/interrupt/disk error after the counts were computed still leaves a
# durable record; it is promoted to `CONSUMED` only after every artifact write
# succeeds. Either state refuses a later `--run-one-shot`, with distinct reason
# codes, so an operator can tell "completed" from "must be investigated".
ONE_SHOT_MARKER_NAME = "ONE_SHOT_CONSUMED.json"
ONE_SHOT_STATE_ARMED = "ARMED"
ONE_SHOT_STATE_CONSUMED = "CONSUMED"
EVIDENCE_ARTIFACT_NAME = "crs24_corr1_evidence.json"
METADATA_ARTIFACT_NAME = "crs24_corr1_launch_metadata.json"

# The literal one-shot confirmation phrase (ROB-974 R2's
# `--confirm-full-corpus-pit` pattern): a caller must echo this back exactly
# to reach `--run-one-shot`'s empirical path. Prevents a copy-pasted/stale
# command line from ever silently reaching the one-shot write path.
ONE_SHOT_CONFIRMATION = "ROB-1040/CRS-24-CORR-1/REAL-CORPUS/OOS-DRY-COUNT-ONE-SHOT"

CLI_USAGE_OR_PLAN_ERROR = 2
LAUNCH_REFUSED = 4


class LaunchRefused(RuntimeError):
    """Safe pre-mutation refusal with a stable, non-secret reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ValueError("closed CLI parse failure")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="run_rob1040_crs24_refreeze")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--run-one-shot", action="store_true")
    parser.add_argument("--manifest")
    parser.add_argument("--corpus-root")
    parser.add_argument("--output-root")
    parser.add_argument("--launcher-sha256")
    parser.add_argument("--confirm-one-shot-oos-dry-count")
    return parser


_COMMON_REQUIRED = ("manifest", "corpus_root", "output_root", "launcher_sha256")


def _dry_run_payload() -> dict[str, object]:
    return {
        "schema_version": "rob1040_crs24_refreeze_launcher_dry_run.v2",
        "run_requested": False,
        "default_state": "DISABLED",
        "description": (
            "No arguments perform no corpus load, artifact write, or broker "
            "call. --preflight verifies gates and builds the real-corpus "
            "input binding without evaluating any feature/gate/count. "
            "--run-one-shot additionally requires HEAD == origin/main (the "
            "merged exact main tree), a clean worktree, and the exact literal "
            "confirmation phrase, and performs the one-shot evaluation."
        ),
        "refreeze_head": CRS24_MERGE_REFREEZE_HEAD,
        "seven_file_seal": {
            "at_merge_commit": CRS24_FILE_DIGESTS_AT_MERGE_COMMIT,
            "current_refreeze": CRS24_CURRENT_REFREEZE_FILE_DIGESTS,
            "changed_files": sorted(CRS24_CHANGED_FILES),
        },
        "authority_file_seal": dict(CRS24_AUTHORITY_FILE_DIGESTS),
        "one_shot_only_gates": [
            "head_is_origin_main",
            "clean_worktree",
            "confirm_phrase",
        ],
        "corpus": {
            "manifest": str(EXPECTED_MANIFEST),
            "corpus_root": str(EXPECTED_CORPUS_ROOT),
            "selected_symbols": list(SELECTED_SYMBOLS),
        },
        "output_root": str(EXPECTED_OUTPUT_ROOT),
        "effects": {
            "corpus_reads": 0,
            "artifact_writes": 0,
            "one_shot_consumed": False,
            "broker_calls": 0,
        },
    }


def _write_json(stream: TextIOBase, payload: Mapping[str, object]) -> None:
    stream.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _physical_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise LaunchRefused("SEALED_FILE_MISSING_OR_UNSAFE") from None
    return digest.hexdigest()


def _require_seven_file_seal(expected: Mapping[str, str]) -> None:
    for name, expected_digest in expected.items():
        actual = _physical_sha256(RESEARCH_ROOT / name)
        if actual != expected_digest:
            raise LaunchRefused(f"SEVEN_FILE_DIGEST_MISMATCH:{name}")


def _require_launcher_self_sha256(claimed: str) -> None:
    actual = _physical_sha256(Path(__file__))
    if actual != claimed:
        raise LaunchRefused("LAUNCHER_PHYSICAL_SHA256_MISMATCH")


def _require_refreeze_head_ancestor() -> None:
    try:
        subprocess.run(
            ("git", "merge-base", "--is-ancestor", CRS24_MERGE_REFREEZE_HEAD, "HEAD"),
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise LaunchRefused("REFREEZE_HEAD_NOT_ANCESTOR") from None


def _require_head_is_origin_main() -> None:
    """`--run-one-shot` may only run on the exact merged `main` tree.

    Linear ROB-1040 실행순서 step 3 requires the final hash-seal/launcher gate
    to be taken "머지된 exact main tree에서". The ancestry check alone is not
    sufficient: it also passes on an unmerged feature branch that merely
    descends from the refreeze head. This gate compares LOCAL refs only (no
    network, no fetch) -- the operator is responsible for having fetched
    `origin/main` before arming the one-shot.

    `--preflight` is exempt (it must stay auditable while the PR is under
    review); the preflight output says so explicitly.
    """
    try:
        head = _git("rev-parse", "HEAD")
        origin_main = _git("rev-parse", "origin/main")
    except (OSError, subprocess.CalledProcessError):
        raise LaunchRefused("GIT_STATE_UNAVAILABLE") from None
    if not head or head != origin_main:
        raise LaunchRefused("HEAD_IS_NOT_ORIGIN_MAIN")


def _measured_head_sha() -> str:
    """The physical `git rev-parse HEAD` at run time.

    The preregistration's stage-3 procedure requires recording "the merge
    commit SHA on `main` that carries this implementation". That SHA cannot be
    known before the merge, so it is not a compile-time constant here; the
    one-shot instead measures it and stamps it into the run metadata, where
    `_require_head_is_origin_main` has already proven it equals `origin/main`.
    """
    try:
        return _git("rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        raise LaunchRefused("GIT_STATE_UNAVAILABLE") from None


def _require_contract_authority() -> None:
    """Run-time re-verification of the fold/filter/contract authorities.

    Two independent layers:

    1. The physical digests of the two reused calendar/fold authority modules
       (outside the preregistered seven-file seal).
    2. ``rob1040_crs24_contracts.validate_contract()``, which recomputes
       `fold_schedule_payload()` / `filter_manifest_payload()` /
       `contract_payload()` from the LIVE authorities and compares them to the
       frozen `FOLD_SCHEDULE_SHA256` / `FILTER_MANIFEST_SHA256` /
       `CONTRACT_SHA256`, plus the exact 8-fold and config-roster assertions.

    Without (2) a drifted calendar authority would be used silently while the
    stale digest was still stamped into all 24 cells.
    """
    for name, expected_digest in CRS24_AUTHORITY_FILE_DIGESTS.items():
        actual = _physical_sha256(RESEARCH_ROOT / name)
        if actual != expected_digest:
            raise LaunchRefused(f"AUTHORITY_FILE_DIGEST_MISMATCH:{name}")
    from rob1040_crs24_contracts import validate_contract

    try:
        validate_contract()
    except (ValueError, TypeError):
        raise LaunchRefused("CONTRACT_AUTHORITY_DRIFTED") from None


def _require_preregistration_artifact() -> None:
    """Physically verify the preregistration `.md` against its frozen pins.

    The sealed CRS-24 modules cannot do this (the static guard forbids
    `open`/`pathlib` inside them) even though they stamp
    `PREREGISTRATION_SHA256` into every cell's `hashes` block. The launcher
    can, so it does -- reading the expected size/digest from
    `rob1040_crs24_contracts` rather than hardcoding a second copy.
    """
    from rob1040_crs24_contracts import (
        PREREGISTRATION_RELATIVE_PATH,
        canonical_preregistration_bytes,
    )

    path = REPO_ROOT / PREREGISTRATION_RELATIVE_PATH
    try:
        raw = path.read_bytes()
    except OSError:
        raise LaunchRefused("PREREGISTRATION_ARTIFACT_MISSING") from None
    try:
        canonical_preregistration_bytes(raw)
    except (ValueError, TypeError):
        raise LaunchRefused("PREREGISTRATION_ARTIFACT_MISMATCH") from None


def _require_clean_worktree() -> None:
    try:
        dirty = _git("status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError):
        raise LaunchRefused("GIT_STATE_UNAVAILABLE") from None
    if dirty:
        raise LaunchRefused("WORKTREE_NOT_CLEAN")


def _require_paths(arguments: argparse.Namespace) -> tuple[Path, Path, Path]:
    try:
        manifest = Path(arguments.manifest).resolve(strict=True)
        corpus_root = Path(arguments.corpus_root).resolve(strict=True)
        output_root = Path(arguments.output_root)
    except (OSError, RuntimeError, TypeError):
        raise LaunchRefused("APPROVED_PATH_RESOLUTION_FAILED") from None
    if manifest != EXPECTED_MANIFEST.resolve(strict=True):
        raise LaunchRefused("MANIFEST_PATH_MISMATCH")
    if corpus_root != EXPECTED_CORPUS_ROOT.resolve(strict=True):
        raise LaunchRefused("CORPUS_ROOT_PATH_MISMATCH")
    if not output_root.is_absolute() or output_root != EXPECTED_OUTPUT_ROOT:
        raise LaunchRefused("OUTPUT_ROOT_PATH_MISMATCH")
    return manifest, corpus_root, output_root


def _install_runtime_paths() -> None:
    for path in (str(RESEARCH_ROOT),):
        if path not in sys.path:
            sys.path.insert(0, path)


def _verify_manifest(manifest_path: Path):  # noqa: ANN201 - CorpusManifest, dynamic import
    """Load+verify the frozen parent manifest via the existing ROB-974 lineage
    seal (`rob974_lineage.verify_parent`) -- reused unmodified, not
    reimplemented. Only reads the small committed JSON manifest, never the
    corpus parquet shards."""
    import rob974_lineage

    if (
        rob974_lineage.PARENT_CONTENT_SHA256 != EXPECTED_PARENT_CONTENT_SHA256
        or rob974_lineage.PARENT_MANIFEST_SHA256 != EXPECTED_PARENT_MANIFEST_SHA256
        or rob974_lineage.SELECTED_UNIVERSE != SELECTED_SYMBOLS
    ):
        raise LaunchRefused("LINEAGE_AUTHORITY_DRIFTED")
    try:
        manifest = rob974_lineage.verify_parent(manifest_path)
    except ValueError:
        raise LaunchRefused("MANIFEST_LINEAGE_MISMATCH") from None
    return manifest


def _require_pit_lookback_coverage(manifest) -> None:  # noqa: ANN001
    """Metadata-only PIT sufficiency check: the first scheduled cutoff's PIT
    lookback (60 calendar days) plus the widest configured formation window
    (CRS-A2, 84 complete 4h returns) must not require history before the
    corpus's declared minimum open_time_ms.

    Two-sided. The upper bound matters just as much: a corpus truncated on the
    RIGHT would not be refused by a lower-bound-only gate -- every missing
    entry/exit reference past the truncation would simply be counted as
    `entry_reference_missing` / `exit_reference_missing` and the run would look
    "valid" while silently under-counting. So the last exit-presence key in the
    frozen domain (last cutoff + 24h + 60s) must also be inside the corpus's
    declared `max_open_time_ms`.

    Uses ONLY the manifest's declared `min_open_time_ms`/`max_open_time_ms`
    per symbol -- no row data is read, no statistic is computed."""
    from rob974_h4_contracts import exact_h4_folds
    from rob1040_crs24_contracts import ACTIVE_CONFIGS, FOUR_HOUR_MS, PIT_LOOKBACK_MS
    from rob1040_crs24_feasibility import (
        expected_exit_presence_keys,
        scheduled_cutoffs,
    )

    first_cutoff = scheduled_cutoffs(exact_h4_folds()[0])[0]
    widest_formation = max(config.formation_return_count for config in ACTIVE_CONFIGS)
    floor_ms = first_cutoff - PIT_LOOKBACK_MS - widest_formation * FOUR_HOUR_MS
    ceiling_ms = max(key.timestamp_ms for key in expected_exit_presence_keys())
    for kline in manifest.klines:
        if kline.symbol not in SELECTED_SYMBOLS:
            continue
        if kline.min_open_time_ms > floor_ms:
            raise LaunchRefused("PIT_LOOKBACK_COVERAGE_INSUFFICIENT")
        if kline.max_open_time_ms < ceiling_ms:
            raise LaunchRefused("PIT_HORIZON_COVERAGE_INSUFFICIENT")


def _build_reference_surface(minute_rows: Mapping[str, tuple]):  # noqa: ANN001, ANN201
    from rob1040_crs24_feasibility import (
        EntryReference,
        ExitPresence,
        ReferenceSurface,
        expected_entry_reference_keys,
        expected_exit_presence_keys,
    )

    by_symbol_ts = {
        symbol: {bar.ts: bar for bar in bars} for symbol, bars in minute_rows.items()
    }
    entries = tuple(
        EntryReference(
            key,
            None
            if by_symbol_ts[key.symbol].get(key.timestamp_ms) is None
            else Decimal(str(by_symbol_ts[key.symbol][key.timestamp_ms].open)),
        )
        for key in expected_entry_reference_keys()
    )
    exit_presence = tuple(
        ExitPresence(key, key.timestamp_ms in by_symbol_ts[key.symbol])
        for key in expected_exit_presence_keys()
    )
    return ReferenceSurface(entries, exit_presence)


def _load_real_corpus_binding(manifest, corpus_root: Path):  # noqa: ANN001, ANN201
    """Fully offline (`rob941_offline_loader`, network-0) real-corpus load.

    Builds the exact two objects the sealed CRS-24 evidence module accepts
    as a caller-supplied binding: a `CRSFeatureGenerator` over real
    complete-4h bars (reusing `rob1040_crs24_features.complete_bars_from_minutes`,
    itself a thin wrapper over the ROB-974 H1 `build_complete_4h` semantics)
    and a `ReferenceSurface` over the same real 1-minute bars' open
    (entry)/presence (exit). No feature/gate/candidate/occupancy evaluation
    happens in this function -- that only happens inside the sealed module's
    `evaluate_cell`, reached only via `.cells()`.
    """
    import rob941_offline_loader
    from rob974_features import MinuteBar
    from rob1040_crs24_evidence import CampaignInputBinding
    from rob1040_crs24_features import CRSFeatureGenerator, complete_bars_from_minutes

    loaded = rob941_offline_loader.load_corpus(manifest, corpus_root)
    klines = loaded["klines"]
    minute_rows: dict[str, tuple] = {}
    for symbol in SELECTED_SYMBOLS:
        rows = klines.get(symbol)
        if rows is None:
            raise LaunchRefused("SELECTED_CORPUS_SYMBOL_MISSING")
        minute_rows[symbol] = tuple(
            MinuteBar(
                row.open_time_ms,
                row.open,
                row.high,
                row.low,
                row.close,
                row.base_volume,
            )
            for row in rows
        )

    bars_4h = complete_bars_from_minutes(minute_rows)
    generator = CRSFeatureGenerator(bars_4h)
    references = _build_reference_surface(minute_rows)
    return CampaignInputBinding.for_real_corpus(
        version=REAL_CORPUS_BINDING_VERSION,
        generator=generator,
        references=references,
        corpus_manifest_content_sha256=manifest.content_hash(),
    )


def _require_one_shot_not_consumed(output_root: Path) -> None:
    """Refuse if the one-shot has been armed or consumed, with distinct codes.

    ``ARMED`` means a previous invocation reached the sealed evaluation but did
    not finish writing its artifacts -- the one-shot may in fact have been
    consumed (the counts computed) with no complete record. That state is NOT
    self-clearing: a human must investigate the output root and explicitly
    resolve it before any further run.
    """
    marker = output_root / ONE_SHOT_MARKER_NAME
    if not marker.exists():
        return
    try:
        state = json.loads(marker.read_text(encoding="utf-8")).get("state")
    except (OSError, ValueError, AttributeError):
        raise LaunchRefused("ONE_SHOT_MARKER_UNREADABLE") from None
    if state == ONE_SHOT_STATE_ARMED:
        raise LaunchRefused("ONE_SHOT_ARMED_NOT_RECONCILED")
    if state == ONE_SHOT_STATE_CONSUMED:
        raise LaunchRefused("ONE_SHOT_ALREADY_CONSUMED")
    raise LaunchRefused("ONE_SHOT_MARKER_UNREADABLE")


def _arm_one_shot_marker(output_root: Path, *, armed_at_utc: str) -> None:
    """Durably record ARMED before the sealed evaluation can compute anything.

    Exclusive-create (``O_CREAT | O_EXCL``) so a concurrent invocation cannot
    both pass the pre-check and both arm; ``fsync`` before returning so a
    crash immediately after arming still leaves the record on disk.
    """
    output_root.mkdir(parents=True, exist_ok=True)
    marker = output_root / ONE_SHOT_MARKER_NAME
    body = (
        json.dumps(
            {
                "state": ONE_SHOT_STATE_ARMED,
                "armed_at_utc": armed_at_utc,
                "note": (
                    "Sealed CRS-24 evaluation was about to begin. If this state "
                    "persists the one-shot may have been consumed without a "
                    "complete artifact set; investigate before any re-run."
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise LaunchRefused("ONE_SHOT_ALREADY_CONSUMED") from None
    except OSError:
        raise LaunchRefused("ONE_SHOT_MARKER_UNWRITABLE") from None
    try:
        os.write(descriptor, body.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _preflight_payload(binding) -> dict[str, object]:  # noqa: ANN001
    identity: dict[str, object] = {
        "posture": binding.posture,
        "version": binding.version,
        "complete_bar_snapshot_sha256": binding.snapshot_sha256_pin,
        "entry_reference_source_sha256": binding.entry_source_sha256_pin,
        "exit_presence_source_sha256": binding.exit_presence_source_sha256_pin,
        "fixture_content_sha256": binding.fixture_content_sha256_pin,
    }
    if binding.extra_authority:
        identity["real_corpus_authority"] = dict(binding.extra_authority)
    return {
        "schema_version": "rob1040_crs24_refreeze_launcher_preflight.v2",
        "mode": "preflight",
        "refreeze_head": CRS24_MERGE_REFREEZE_HEAD,
        "seven_file_seal_verified": True,
        "authority_file_seal_verified": True,
        "contract_authority_revalidated": True,
        "preregistration_artifact_verified": True,
        "head_is_origin_main_enforced": False,
        "head_is_origin_main_note": (
            "HEAD == origin/main is enforced ONLY by --run-one-shot; "
            "--preflight is deliberately exempt so it stays auditable while "
            "this PR is under review."
        ),
        "binding_identity": identity,
        "counts_computed": False,
        "effects": {"artifact_writes": 0, "one_shot_consumed": False},
    }


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _run_one_shot_payload(
    *,
    arguments: argparse.Namespace,
    binding,  # noqa: ANN001
    evidence,  # noqa: ANN001
    output_root: Path,
    run_started_at_utc: str,
    measured_head_sha: str,
) -> tuple[dict[str, object], dict[str, object]]:
    evidence_payload = evidence.to_payload()
    metadata_payload: dict[str, object] = {
        "schema_version": "rob1040_crs24_refreeze_launcher_metadata.v2",
        "run_started_at_utc": run_started_at_utc,
        "refreeze_head": CRS24_MERGE_REFREEZE_HEAD,
        # Stage-3 procedure item 1 ("the merge commit SHA on main that carries
        # this implementation"). It cannot be a compile-time constant -- the
        # merge SHA is unknowable before the merge -- so it is MEASURED here,
        # after `_require_head_is_origin_main` proved HEAD == origin/main.
        "head_sha_at_run": measured_head_sha,
        "head_is_origin_main_verified": True,
        "seven_file_seal": {
            "at_merge_commit": CRS24_FILE_DIGESTS_AT_MERGE_COMMIT,
            "current_refreeze": CRS24_CURRENT_REFREEZE_FILE_DIGESTS,
            "changed_files": sorted(CRS24_CHANGED_FILES),
        },
        # Separate from the preregistered seven-file seal on purpose.
        "authority_file_seal": dict(CRS24_AUTHORITY_FILE_DIGESTS),
        "contract_authority_revalidated": True,
        "preregistration_artifact_verified": True,
        "corpus": {
            "manifest": str(EXPECTED_MANIFEST),
            "corpus_root": str(EXPECTED_CORPUS_ROOT),
            "manifest_content_sha256": EXPECTED_PARENT_CONTENT_SHA256,
        },
        "binding_posture": binding.posture,
        "binding_version": binding.version,
        "evidence_sha256": evidence.evidence_sha256,
        "launcher_argv": {
            "manifest": arguments.manifest,
            "corpus_root": arguments.corpus_root,
            "output_root": arguments.output_root,
            "launcher_sha256": arguments.launcher_sha256,
        },
        "artifacts": {
            "evidence": str(output_root / EVIDENCE_ARTIFACT_NAME),
            "metadata": str(output_root / METADATA_ARTIFACT_NAME),
            "one_shot_marker": str(output_root / ONE_SHOT_MARKER_NAME),
        },
    }
    return evidence_payload, metadata_payload


def _execute_preflight(
    arguments: argparse.Namespace, *, stdout: TextIOBase, stderr: TextIOBase
) -> int:
    _require_launcher_self_sha256(arguments.launcher_sha256)
    stderr.write("REFREEZE_PREFLIGHT launcher_self_sha256=PASS\n")
    _require_refreeze_head_ancestor()
    stderr.write("REFREEZE_PREFLIGHT refreeze_head_ancestor=PASS\n")
    _require_seven_file_seal(CRS24_CURRENT_REFREEZE_FILE_DIGESTS)
    stderr.write("REFREEZE_PREFLIGHT seven_file_seal=PASS\n")
    manifest_path, corpus_root, _output_root = _require_paths(arguments)
    _install_runtime_paths()
    _require_contract_authority()
    stderr.write("REFREEZE_PREFLIGHT contract_authority=PASS\n")
    _require_preregistration_artifact()
    stderr.write("REFREEZE_PREFLIGHT preregistration_artifact=PASS\n")
    stderr.write(
        "REFREEZE_PREFLIGHT head_is_origin_main=EXEMPT_IN_PREFLIGHT "
        "(enforced only in --run-one-shot)\n"
    )
    manifest = _verify_manifest(manifest_path)
    stderr.write("REFREEZE_PREFLIGHT manifest_lineage=PASS\n")
    _require_pit_lookback_coverage(manifest)
    stderr.write("REFREEZE_PREFLIGHT pit_lookback_coverage=PASS\n")
    binding = _load_real_corpus_binding(manifest, corpus_root)
    stderr.write("REFREEZE_PREFLIGHT real_corpus_binding_built=PASS\n")

    from rob1040_crs24_evidence import open_real_corpus_campaign_context

    # Opens+identity-checks the sealed context but never calls `.cells()` --
    # no feature/gate/candidate/occupancy evaluation, no OOS incidence count.
    open_real_corpus_campaign_context(binding)
    stderr.write("REFREEZE_PREFLIGHT validated_context_opened=PASS\n")

    _write_json(stdout, _preflight_payload(binding))
    return 0


def _execute_run_one_shot(
    arguments: argparse.Namespace, *, stdout: TextIOBase, stderr: TextIOBase
) -> int:
    from datetime import UTC, datetime

    if arguments.confirm_one_shot_oos_dry_count != ONE_SHOT_CONFIRMATION:
        raise LaunchRefused("CONFIRM_PHRASE_MISMATCH")
    _require_launcher_self_sha256(arguments.launcher_sha256)
    _require_refreeze_head_ancestor()
    _require_head_is_origin_main()
    _require_clean_worktree()
    _require_seven_file_seal(CRS24_CURRENT_REFREEZE_FILE_DIGESTS)
    manifest_path, corpus_root, output_root = _require_paths(arguments)
    _install_runtime_paths()
    _require_contract_authority()
    _require_preregistration_artifact()
    _require_one_shot_not_consumed(output_root)
    measured_head_sha = _measured_head_sha()
    stderr.write("REFREEZE_ONE_SHOT static_gates=PASS\n")

    manifest = _verify_manifest(manifest_path)
    _require_pit_lookback_coverage(manifest)
    binding = _load_real_corpus_binding(manifest, corpus_root)
    stderr.write("REFREEZE_ONE_SHOT real_corpus_binding_built=PASS\n")

    # Re-check the marker immediately before the empirical evaluation too --
    # narrows (never eliminates) a TOCTOU window against a concurrent run.
    _require_one_shot_not_consumed(output_root)

    from rob1040_crs24_evidence import build_real_corpus_evidence

    run_started_at_utc = datetime.now(UTC).isoformat()
    # Durably ARM the one-shot BEFORE any count can be computed: after this
    # point a crash, interrupt or write failure still leaves a record, so the
    # "exactly once" contract no longer depends on the happy path.
    _arm_one_shot_marker(output_root, armed_at_utc=run_started_at_utc)
    stderr.write("REFREEZE_ONE_SHOT one_shot_marker=ARMED\n")
    stderr.write("REFREEZE_ONE_SHOT evaluating_sealed_campaign starting\n")
    evidence = build_real_corpus_evidence(binding)
    stderr.write("REFREEZE_ONE_SHOT evaluating_sealed_campaign PASS\n")

    evidence_payload, metadata_payload = _run_one_shot_payload(
        arguments=arguments,
        binding=binding,
        evidence=evidence,
        output_root=output_root,
        run_started_at_utc=run_started_at_utc,
        measured_head_sha=measured_head_sha,
    )

    _atomic_write_json(output_root / EVIDENCE_ARTIFACT_NAME, evidence_payload)
    _atomic_write_json(output_root / METADATA_ARTIFACT_NAME, metadata_payload)
    _atomic_write_json(
        output_root / ONE_SHOT_MARKER_NAME,
        {
            "state": ONE_SHOT_STATE_CONSUMED,
            "armed_at_utc": run_started_at_utc,
            "consumed_at_utc": run_started_at_utc,
            "head_sha_at_run": measured_head_sha,
            "evidence_sha256": evidence.evidence_sha256,
        },
    )
    stderr.write("REFREEZE_ONE_SHOT one_shot_marker=CONSUMED\n")

    _write_json(
        stdout,
        {
            "schema_version": "rob1040_crs24_refreeze_launcher_result.v1",
            "mode": "run_one_shot",
            "evidence_sha256": evidence.evidence_sha256,
            "artifacts": metadata_payload["artifacts"],
        },
    )
    return 0


def run_cli(
    argv: Sequence[str],
    *,
    stdout: TextIOBase,
    stderr: TextIOBase,
) -> int:
    """Run the closed launcher without calling ``sys.exit``."""
    if isinstance(argv, str | bytes) or not isinstance(argv, Sequence):
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR
    if not argv:
        _write_json(stdout, _dry_run_payload())
        return 0
    try:
        arguments = _parser().parse_args(list(argv))
    except (TypeError, ValueError):
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR

    if arguments.preflight:
        if any(getattr(arguments, name) is None for name in _COMMON_REQUIRED):
            stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
            return CLI_USAGE_OR_PLAN_ERROR
        executor = _execute_preflight
    elif arguments.run_one_shot:
        required = (*_COMMON_REQUIRED, "confirm_one_shot_oos_dry_count")
        if any(getattr(arguments, name) is None for name in required):
            stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
            return CLI_USAGE_OR_PLAN_ERROR
        executor = _execute_run_one_shot
    else:
        stderr.write("CLI_USAGE_OR_PLAN_ERROR\n")
        return CLI_USAGE_OR_PLAN_ERROR

    try:
        return executor(arguments, stdout=stdout, stderr=stderr)
    except LaunchRefused as exc:
        stderr.write("LAUNCH_REFUSED " + exc.reason_code + "\n")
        return LAUNCH_REFUSED
    except KeyboardInterrupt:
        stderr.write("INTERRUPTED audit_state_before_retry\n")
        return 130
    except Exception as exc:
        stderr.write(f"LAUNCH_REFUSED UNEXPECTED_{type(exc).__name__}\n")
        return LAUNCH_REFUSED


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(
        tuple(sys.argv[1:] if argv is None else argv),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
