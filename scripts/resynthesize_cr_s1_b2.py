"""Recompose CR-S1 top-level B2 labels from fixed completed arm artifacts.

This is intentionally not a backtest runner: it does not import a corpus
loader or the execution engine.  It SHA-checks the seven relay-pinned source
files, reads their already-produced annual arm metrics, and writes one fresh,
no-overwrite result outside the source directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from research.crypto_stage_b.resynthesis import (
    resynthesize_artifact_directory,
    sha256_file,
    write_json_once,
)

_DEFAULT_OUTPUT_FILENAME = "cr-s1-b2-top-level-resynthesis.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-filename", default=_DEFAULT_OUTPUT_FILENAME)
    args = parser.parse_args(argv)

    source_directory = args.source_dir.resolve()
    output_directory = args.output_dir.resolve()
    if output_directory == source_directory or output_directory.is_relative_to(
        source_directory
    ):
        raise ValueError("output directory must be a new path outside source-dir")

    payload = resynthesize_artifact_directory(source_directory)
    output_path = output_directory / args.output_filename
    output_sha256 = write_json_once(output_path, payload)
    print(
        json.dumps(
            {
                "output_path": str(output_path),
                "output_sha256": output_sha256,
                "record_count": len(payload["records"]),
                "resynthesis_only": payload["resynthesis_only"],
                "source_manifest_sha256": sha256_file(
                    source_directory / "manifest.json"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
