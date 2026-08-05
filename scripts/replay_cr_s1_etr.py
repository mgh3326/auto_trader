"""Hash-bound, no-overwrite replay driver for the two CR-S1 ETR venue pairs.

The driver is deliberately offline and scheduleless.  It accepts only the
already-authorized ETR candidate, one already-labeled venue corpus, and the
frozen exploration window/cost literal for that venue.  It creates one pair
record through an atomic hard-link publication, refusing to replace any prior
artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from research.crypto_corpus.loader import load_labeled_parquet_files
from research.crypto_stage_b.contracts import (
    FROZEN_VENUE_COST_LITERALS,
    CryptoStageBRunContract,
    VenueCostLiteral,
)
from research.crypto_stage_b.engine import CandidatePairResult, run_candidate_pair
from research.crypto_stage_b.registry import CandidateRegistry
from research.crypto_stage_b.report import build_harness_report
from research.crypto_stage_b.source import source_from_labeled_corpus

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DRIVER_PATH = Path(__file__).resolve()
_ETR_STRATEGY_ID = "CR-SPOT-ETR-01"


@dataclass(frozen=True)
class _FrozenPairSpec:
    venue: str
    exploration_start: date
    exploration_end: date
    output_filename: str


_FROZEN_PAIR_SPECS = {
    "upbit_krw": _FrozenPairSpec(
        venue="upbit_krw",
        exploration_start=date(2017, 10, 24),
        exploration_end=date(2024, 12, 31),
        output_filename="cr-spot-etr-01__upbit_krw.json",
    ),
    "binance_usdt_spot": _FrozenPairSpec(
        venue="binance_usdt_spot",
        exploration_start=date(2018, 3, 19),
        exploration_end=date(2024, 12, 31),
        output_filename="cr-spot-etr-01__binance_usdt_spot.json",
    ),
}

_CODE_BLOB_PATHS = (
    Path("research/crypto_stage_b/contracts.py"),
    Path("research/crypto_stage_b/engine.py"),
    Path("research/crypto_stage_b/registry.py"),
    Path("research/crypto_stage_b/report.py"),
    Path("research/crypto_stage_b/signals.py"),
    Path("research/crypto_stage_b/source.py"),
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_json(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _code_blob_manifest() -> dict[str, str]:
    return {
        path.as_posix(): _sha256_file(_REPO_ROOT / path) for path in _CODE_BLOB_PATHS
    }


def _code_blob_manifest_sha256(manifest: dict[str, str]) -> str:
    return _sha256_json(manifest)


def _git_revision() -> str | None:
    result = subprocess.run(
        ("git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _git_blob_sha1(path: Path) -> str | None:
    result = subprocess.run(
        ("git", "-C", str(_REPO_ROOT), "hash-object", str(path)),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def pair_record_stream_sha256(pair: CandidatePairResult) -> str:
    """Hash the explicitly named, complete deterministic pair-record stream."""
    return _sha256_json(pair.to_dict())


def write_json_once(path: Path, payload: dict[str, Any]) -> str:
    """Atomically publish one JSON file without exposing or replacing a final.

    A same-directory temporary is fully fsynced, then hard-linked to the final
    name.  ``link`` fails if that final name already exists, so concurrent or
    repeated runs cannot overwrite a completed pair record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _canonical_json_bytes(payload)
    digest = hashlib.sha256(rendered).hexdigest()
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.partial"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite completed pair record: {path}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return digest


def build_pair_record(
    *,
    candidate_return: Path,
    labeled_parquet: Sequence[Path],
    venue: str,
    expected_driver_sha256: str,
    expected_code_manifest_sha256: str,
) -> tuple[str, dict[str, Any]]:
    """Run one frozen ETR pair only after all replay-code bindings match."""
    spec = _FROZEN_PAIR_SPECS[venue]
    driver_sha256 = _sha256_file(_DRIVER_PATH)
    if driver_sha256 != expected_driver_sha256:
        raise ValueError("replay driver SHA-256 mismatch; refuse an unbound replay")
    code_blob_manifest = _code_blob_manifest()
    code_manifest_sha256 = _code_blob_manifest_sha256(code_blob_manifest)
    if code_manifest_sha256 != expected_code_manifest_sha256:
        raise ValueError(
            "engine code-manifest SHA-256 mismatch; refuse an unbound replay"
        )

    registry = CandidateRegistry.load(candidate_return)
    candidate = next(
        (
            definition
            for definition in registry.admitted
            if definition.strategy_id == _ETR_STRATEGY_ID
        ),
        None,
    )
    if candidate is None:  # pragma: no cover - admission invariant guard
        raise ValueError(f"frozen registry does not admit {_ETR_STRATEGY_ID}")
    corpus = load_labeled_parquet_files(labeled_parquet, consumer_intent="time_series")
    if corpus.policy.venue != venue:
        raise ValueError(
            f"labeled corpus venue {corpus.policy.venue!r} is not requested {venue!r}"
        )
    source = source_from_labeled_corpus(
        corpus,
        exploration_start=spec.exploration_start,
        exploration_end=spec.exploration_end,
    )
    contract = CryptoStageBRunContract(
        candidate=candidate,
        venue=venue,
        exploration_start=spec.exploration_start,
        exploration_end=spec.exploration_end,
        cost=VenueCostLiteral(venue=venue, **_frozen_cost_fields(venue)),
    )
    pair = run_candidate_pair(source=source, contract=contract)
    pair_payload = pair.to_dict()
    pair_record_stream_hash = pair_record_stream_sha256(pair)
    record = {
        "schema_version": "cr-s1-etr-replay-v1",
        "strategy_id": _ETR_STRATEGY_ID,
        "venue": venue,
        "engine_revision": _git_revision(),
        "driver": {
            "path": _DRIVER_PATH.relative_to(_REPO_ROOT).as_posix(),
            "blob_sha256": driver_sha256,
            "git_blob_sha1": _git_blob_sha1(_DRIVER_PATH),
        },
        "engine_code_blobs": code_blob_manifest,
        "engine_code_manifest_sha256": code_manifest_sha256,
        "candidate_return": {
            "path": str(candidate_return),
            "sha256": _sha256_file(candidate_return),
        },
        "labeled_corpus": [
            {"path": str(path), "sha256": _sha256_file(path)}
            for path in labeled_parquet
        ],
        "terminal_events_input_count": 0,
        "run_contract": contract.to_dict(),
        "pair_record_stream_sha256": pair_record_stream_hash,
        "pair": pair_payload,
        "harness_query": build_harness_report(pair).to_dict(),
    }
    return spec.output_filename, record


def _frozen_cost_fields(venue: str) -> dict[str, int]:
    try:
        return dict(FROZEN_VENUE_COST_LITERALS[venue])
    except KeyError as exc:
        raise ValueError(f"unsupported frozen venue: {venue!r}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-return", type=Path, required=True)
    parser.add_argument("--labeled-parquet", type=Path, action="append", required=True)
    parser.add_argument("--venue", choices=tuple(_FROZEN_PAIR_SPECS), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-driver-sha256", required=True)
    parser.add_argument("--expected-code-manifest-sha256", required=True)
    args = parser.parse_args(argv)

    output_filename, record = build_pair_record(
        candidate_return=args.candidate_return,
        labeled_parquet=tuple(args.labeled_parquet),
        venue=args.venue,
        expected_driver_sha256=args.expected_driver_sha256,
        expected_code_manifest_sha256=args.expected_code_manifest_sha256,
    )
    output_path = args.output_dir / output_filename
    output_sha256 = write_json_once(output_path, record)
    print(
        json.dumps(
            {
                "output_path": str(output_path),
                "output_sha256": output_sha256,
                "pair_record_stream_sha256": record["pair_record_stream_sha256"],
                "driver_blob_sha256": record["driver"]["blob_sha256"],
                "driver_git_blob_sha1": record["driver"]["git_blob_sha1"],
                "engine_code_manifest_sha256": record["engine_code_manifest_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
