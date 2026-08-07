#!/usr/bin/env python3
"""Execute the D3-C2G B0 calibration 2-physical replay (Amendment A2 scope)."""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import replace
from pathlib import Path

from research.kr_corpus.d3_engine.calibration_run import (
    CalibrationHarnessPaths,
    CalibrationRunHarness,
)
from research.kr_corpus.d3_engine.canonical import canonical_bytes


def _git(*args: str) -> str:
    return subprocess.run(
        ("git", *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--progress-report", type=Path)
    parser.add_argument(
        "--harness-commit",
        help="full committed SHA of this harness (default: current HEAD)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="stamp the current HEAD while the tree is still dirty (rehearsal only)",
    )
    args = parser.parse_args()

    head = _git("rev-parse", "HEAD")
    harness_commit = args.harness_commit or head
    if harness_commit != head:
        parser.error(f"--harness-commit {harness_commit} does not match HEAD {head}")
    if _git("status", "--porcelain") and not args.allow_dirty:
        parser.error(
            "repository must be clean so the stamped commit identifies the code"
        )

    paths = CalibrationHarnessPaths.defaults()
    paths = replace(
        paths,
        output_root=args.output_root or paths.output_root,
        progress_report=args.progress_report or paths.progress_report,
    )
    result = CalibrationRunHarness(
        harness_commit=harness_commit,
        paths=paths,
    ).run_all()
    print(canonical_bytes(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
