"""Hash-bound, no-overwrite correction replay driver for four CR-S1 pairs.

The driver is offline and scheduleless.  It accepts only the four authorized
TPR/CEB candidate × venue pairs, runs one pair per invocation after binding the
driver and engine hashes, and atomically publishes immutable JSON records.
``--write-manifest`` then seals exactly those four records; ETR is not an
accepted target of any invocation.
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
_BUDGET_LITERAL = (
    "이 replay 는 기존 구현 오류의 correction 이며 새 trial 이 아니다 — D2 예산 미소모."
)


@dataclass(frozen=True)
class _FrozenPairSpec:
    strategy_id: str
    venue: str
    exploration_start: date
    exploration_end: date
    output_filename: str


_FROZEN_PAIR_SPECS = {
    "tpr_upbit_krw": _FrozenPairSpec(
        strategy_id="CR-SPOT-TPR-01",
        venue="upbit_krw",
        exploration_start=date(2017, 10, 24),
        exploration_end=date(2024, 12, 31),
        output_filename="cr-spot-tpr-01__upbit_krw.json",
    ),
    "tpr_binance_usdt_spot": _FrozenPairSpec(
        strategy_id="CR-SPOT-TPR-01",
        venue="binance_usdt_spot",
        exploration_start=date(2018, 3, 19),
        exploration_end=date(2024, 12, 31),
        output_filename="cr-spot-tpr-01__binance_usdt_spot.json",
    ),
    "ceb_upbit_krw": _FrozenPairSpec(
        strategy_id="CR-SPOT-CEB-01",
        venue="upbit_krw",
        exploration_start=date(2017, 10, 24),
        exploration_end=date(2024, 12, 31),
        output_filename="cr-spot-ceb-01__upbit_krw.json",
    ),
    "ceb_binance_usdt_spot": _FrozenPairSpec(
        strategy_id="CR-SPOT-CEB-01",
        venue="binance_usdt_spot",
        exploration_start=date(2018, 3, 19),
        exploration_end=date(2024, 12, 31),
        output_filename="cr-spot-ceb-01__binance_usdt_spot.json",
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
    """Atomically publish one JSON file and refuse to replace a completed file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _canonical_json_bytes(payload)
    digest = hashlib.sha256(rendered).hexdigest()
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.partial"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite completed replay record: {path}"
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


def _bound_code_manifest(
    *, expected_driver_sha256: str, expected_code_manifest_sha256: str
) -> tuple[str, dict[str, str], str]:
    driver_sha256 = _sha256_file(_DRIVER_PATH)
    if driver_sha256 != expected_driver_sha256:
        raise ValueError("replay driver SHA-256 mismatch; refuse an unbound replay")
    code_blob_manifest = _code_blob_manifest()
    code_manifest_sha256 = _code_blob_manifest_sha256(code_blob_manifest)
    if code_manifest_sha256 != expected_code_manifest_sha256:
        raise ValueError(
            "engine code-manifest SHA-256 mismatch; refuse an unbound replay"
        )
    return driver_sha256, code_blob_manifest, code_manifest_sha256


def _frozen_cost_fields(venue: str) -> dict[str, int]:
    try:
        return dict(FROZEN_VENUE_COST_LITERALS[venue])
    except KeyError as exc:
        raise ValueError(f"unsupported frozen venue: {venue!r}") from exc


def build_pair_record(
    *,
    pair_name: str,
    candidate_return: Path,
    labeled_parquet: Sequence[Path],
    expected_driver_sha256: str,
    expected_code_manifest_sha256: str,
) -> tuple[str, dict[str, Any]]:
    """Run exactly one authorized TPR/CEB pair after binding all replay code."""
    spec = _FROZEN_PAIR_SPECS[pair_name]
    driver_sha256, code_blob_manifest, code_manifest_sha256 = _bound_code_manifest(
        expected_driver_sha256=expected_driver_sha256,
        expected_code_manifest_sha256=expected_code_manifest_sha256,
    )

    registry = CandidateRegistry.load(candidate_return)
    candidate = registry.get(spec.strategy_id)
    corpus = load_labeled_parquet_files(labeled_parquet, consumer_intent="time_series")
    if corpus.policy.venue != spec.venue:
        raise ValueError(
            f"labeled corpus venue {corpus.policy.venue!r} is not requested "
            f"{spec.venue!r}"
        )
    source = source_from_labeled_corpus(
        corpus,
        exploration_start=spec.exploration_start,
        exploration_end=spec.exploration_end,
    )
    contract = CryptoStageBRunContract(
        candidate=candidate,
        venue=spec.venue,
        exploration_start=spec.exploration_start,
        exploration_end=spec.exploration_end,
        cost=VenueCostLiteral(venue=spec.venue, **_frozen_cost_fields(spec.venue)),
    )
    pair = run_candidate_pair(source=source, contract=contract)
    record = {
        "schema_version": "cr-s1-tpr-ceb-correction-replay-v1",
        "replay_classification": {
            "budget_literal": _BUDGET_LITERAL,
            "trial_count": 0,
            "correction_replay_count": 1,
        },
        "strategy_id": spec.strategy_id,
        "venue": spec.venue,
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
        "pair_record_stream_sha256": pair_record_stream_sha256(pair),
        "pair": pair.to_dict(),
        "harness_query": build_harness_report(pair).to_dict(),
    }
    return spec.output_filename, record


def build_manifest(
    *,
    output_dir: Path,
    expected_driver_sha256: str,
    expected_code_manifest_sha256: str,
) -> dict[str, Any]:
    """Seal precisely the four authorized correction records, never ETR."""
    driver_sha256, code_blob_manifest, code_manifest_sha256 = _bound_code_manifest(
        expected_driver_sha256=expected_driver_sha256,
        expected_code_manifest_sha256=expected_code_manifest_sha256,
    )
    records: list[dict[str, Any]] = []
    for pair_name, spec in _FROZEN_PAIR_SPECS.items():
        path = output_dir / spec.output_filename
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"required replay record is absent: {path}") from exc
        if (
            payload.get("strategy_id") != spec.strategy_id
            or payload.get("venue") != spec.venue
        ):
            raise ValueError(f"replay record does not match frozen pair: {path}")
        if payload.get("driver", {}).get("blob_sha256") != driver_sha256:
            raise ValueError(f"replay record has an unbound driver: {path}")
        if payload.get("engine_code_manifest_sha256") != code_manifest_sha256:
            raise ValueError(f"replay record has an unbound engine manifest: {path}")
        if payload.get("replay_classification") != {
            "budget_literal": _BUDGET_LITERAL,
            "trial_count": 0,
            "correction_replay_count": 1,
        }:
            raise ValueError(f"replay record has invalid correction accounting: {path}")
        records.append(
            {
                "pair_name": pair_name,
                "strategy_id": spec.strategy_id,
                "venue": spec.venue,
                "output_file": spec.output_filename,
                "output_sha256": _sha256_file(path),
                "pair_record_stream_sha256": payload["pair_record_stream_sha256"],
                "contract_hash": payload["run_contract"]["contract_hash"],
                "cost_round_trip_bp": payload["run_contract"]["cost_literal"].get(
                    "round_trip_bp"
                ),
                "sensitivity_cost_round_trip_bp": payload["run_contract"][
                    "cost_literal"
                ].get("sensitivity_round_trip_bp"),
            }
        )
    return {
        "schema_version": "cr-s1-tpr-ceb-correction-manifest-v1",
        "replay_classification": {
            "budget_literal": _BUDGET_LITERAL,
            "trial_count": 0,
            "correction_replay_count": 4,
        },
        "scope": {
            "authorized_pairs": [
                "CR-SPOT-TPR-01 × upbit_krw",
                "CR-SPOT-TPR-01 × binance_usdt_spot",
                "CR-SPOT-CEB-01 × upbit_krw",
                "CR-SPOT-CEB-01 × binance_usdt_spot",
            ],
            "etr_pairs_touched": 0,
            "additional_pairs_or_venues": 0,
            "holdout_reads": 0,
            "orders": 0,
            "account_mutations": 0,
            "database_writes": 0,
            "scheduler_registrations": 0,
        },
        "engine_revision": _git_revision(),
        "driver": {
            "path": _DRIVER_PATH.relative_to(_REPO_ROOT).as_posix(),
            "blob_sha256": driver_sha256,
            "git_blob_sha1": _git_blob_sha1(_DRIVER_PATH),
        },
        "engine_code_blobs": code_blob_manifest,
        "engine_code_manifest_sha256": code_manifest_sha256,
        "records": records,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--pair", choices=tuple(_FROZEN_PAIR_SPECS))
    action.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--candidate-return", type=Path)
    parser.add_argument("--labeled-parquet", type=Path, action="append")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-driver-sha256", required=True)
    parser.add_argument("--expected-code-manifest-sha256", required=True)
    args = parser.parse_args(argv)

    if args.write_manifest:
        manifest = build_manifest(
            output_dir=args.output_dir,
            expected_driver_sha256=args.expected_driver_sha256,
            expected_code_manifest_sha256=args.expected_code_manifest_sha256,
        )
        output_path = args.output_dir / "manifest.json"
        output_sha256 = write_json_once(output_path, manifest)
        print(
            json.dumps(
                {
                    "output_path": str(output_path),
                    "output_sha256": output_sha256,
                    "driver_blob_sha256": manifest["driver"]["blob_sha256"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    if args.candidate_return is None or not args.labeled_parquet:
        parser.error("--pair requires --candidate-return and --labeled-parquet")
    output_filename, record = build_pair_record(
        pair_name=args.pair,
        candidate_return=args.candidate_return,
        labeled_parquet=tuple(args.labeled_parquet),
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
