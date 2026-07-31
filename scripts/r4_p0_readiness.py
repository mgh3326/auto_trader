"""Run the fixed seven-day R4 P0 readiness audit without network or writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.brokers.binance.r4_p0_collector import (
    REQUIRED_ACTIVE_SOURCES,
    SIGNAL_SYMBOLS,
    runtime_code_hash,
)
from app.services.brokers.binance.r4_p0_hardening import (
    StudyManifestError,
    load_study_manifest,
)
from app.services.brokers.binance.r4_p0_readiness import audit_readiness


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "offline/read-only fixed 7-day R4 P0 readiness auditor; "
            "never starts a collector or watchdog"
        )
    )
    parser.add_argument(
        "--artifact",
        action="append",
        required=True,
        help="collector SQLite artifact path; repeat exactly twice",
    )
    parser.add_argument(
        "--study-manifest",
        required=True,
        help="absolute path to the pinned study manifest",
    )
    parser.add_argument(
        "--study-manifest-sha256",
        required=True,
        help="external SHA-256 pin for the canonical study manifest",
    )
    parser.add_argument(
        "--expected-code-hash",
        help=(
            "expected deployed Git hash; defaults to the hash of this loaded checkout"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = load_study_manifest(
            Path(args.study_manifest),
            expected_sha256=args.study_manifest_sha256,
            expected_sources=tuple(sorted(REQUIRED_ACTIVE_SOURCES)),
            expected_symbols=tuple(sorted(SIGNAL_SYMBOLS)),
        )
        report = audit_readiness(
            tuple(Path(path) for path in args.artifact),
            manifest,
            expected_code_hash=(
                args.expected_code_hash
                if args.expected_code_hash is not None
                else runtime_code_hash()
            ),
        )
    except StudyManifestError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "summary": "FAIL: study manifest could not be verified",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
