"""Recompose CR-S1 B2 labels from the six sealed correction-era artifacts.

This is an artifact-only operation.  It is deliberately bounded to the four
TPR/CEB correction records and the two previously verified ETR records below;
it neither loads a corpus nor invokes the Stage-B engine.  Every source byte
is SHA-256 bound before parsing and publication is no-overwrite.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from research.crypto_stage_b.resynthesis import (
    resynthesize_artifact_files,
    write_json_once,
)

_CORRECTION_OUTPUT_DIRECTORY = Path(
    "/Users/mgh3326/work/herdr-inbox/jobs/"
    "cr-s1-conform-tpr-ceb-20260806-1510/events/run-output"
)
_ETR_OUTPUT_DIRECTORY = Path(
    "/Users/mgh3326/work/herdr-inbox/jobs/"
    "cr-s1-b1-conformance-20260805-2332/events/run-output"
)
_DEFAULT_OUTPUT_FILENAME = "cr-s1-b2-top-level-resynthesis-correction.json"

# These are the only six candidate × venue records permitted in this B2
# composition.  No CLI option can add a pair, venue, or replacement artifact.
_FROZEN_PAIR_SOURCES: dict[str, Path] = {
    "cr-spot-tpr-01__upbit_krw.json": _CORRECTION_OUTPUT_DIRECTORY
    / "cr-spot-tpr-01__upbit_krw.json",
    "cr-spot-tpr-01__binance_usdt_spot.json": _CORRECTION_OUTPUT_DIRECTORY
    / "cr-spot-tpr-01__binance_usdt_spot.json",
    "cr-spot-ceb-01__upbit_krw.json": _CORRECTION_OUTPUT_DIRECTORY
    / "cr-spot-ceb-01__upbit_krw.json",
    "cr-spot-ceb-01__binance_usdt_spot.json": _CORRECTION_OUTPUT_DIRECTORY
    / "cr-spot-ceb-01__binance_usdt_spot.json",
    "cr-spot-etr-01__upbit_krw.json": _ETR_OUTPUT_DIRECTORY
    / "cr-spot-etr-01__upbit_krw.json",
    "cr-spot-etr-01__binance_usdt_spot.json": _ETR_OUTPUT_DIRECTORY
    / "cr-spot-etr-01__binance_usdt_spot.json",
}
_EXPECTED_PAIR_SHA256 = {
    "cr-spot-tpr-01__upbit_krw.json": (
        "0b705ee6df65bceb20e4a2a8d755ef272c6cdd7d5fef9c8d31147ec455d25bc7"
    ),
    "cr-spot-tpr-01__binance_usdt_spot.json": (
        "bcbc3538c224a23b8c0656c9527dd4eda990ad494e0bfc7f60751efab2f3aa1f"
    ),
    "cr-spot-ceb-01__upbit_krw.json": (
        "a8397a8fe927029faa4cc3444d921def7ae5d8ae66e61c9eb26d723639b929f3"
    ),
    "cr-spot-ceb-01__binance_usdt_spot.json": (
        "9cf299de9764471f022c6e50b5d5a1f30f100ab66a5e8880f7e15899f41cec51"
    ),
    "cr-spot-etr-01__upbit_krw.json": (
        "dd9ee78db25ee803395cfb3cfbfa7f85ccdc997abd51aebb583f614cce825c39"
    ),
    "cr-spot-etr-01__binance_usdt_spot.json": (
        "054ce0523889bce47cc62b88565833aa3dbb28632f9c256c8c63a8996b8bb6b3"
    ),
}
_FROZEN_MANIFEST_SOURCES: dict[str, Path] = {
    "tpr_ceb_correction_manifest.json": _CORRECTION_OUTPUT_DIRECTORY / "manifest.json",
    "etr_b1_manifest.json": _ETR_OUTPUT_DIRECTORY / "manifest.json",
}
_EXPECTED_MANIFEST_SHA256 = {
    "tpr_ceb_correction_manifest.json": (
        "f48bd36513bdce5304e356848f973d48f9a443b441642285c7c7218ff0bc2f98"
    ),
    "etr_b1_manifest.json": (
        "85ea79a2571f1c1ba039485d537748dafc7d96ae18fb9ba54155f3acfad4c24b"
    ),
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-filename", default=_DEFAULT_OUTPUT_FILENAME)
    args = parser.parse_args(argv)

    output_directory = args.output_dir.resolve()
    _refuse_source_output_directory(output_directory)
    payload = resynthesize_artifact_files(
        pair_sources=_FROZEN_PAIR_SOURCES,
        expected_pair_sha256=_EXPECTED_PAIR_SHA256,
        manifest_sources=_FROZEN_MANIFEST_SOURCES,
        expected_manifest_sha256=_EXPECTED_MANIFEST_SHA256,
    )
    output_path = output_directory / args.output_filename
    output_sha256 = write_json_once(output_path, payload)
    print(
        json.dumps(
            {
                "output_path": str(output_path),
                "output_sha256": output_sha256,
                "record_count": len(payload["records"]),
                "resynthesis_only": payload["resynthesis_only"],
                "source_manifest_sha256": {
                    item["label"]: item["sha256"]
                    for item in payload["source_manifests"]
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _refuse_source_output_directory(output_directory: Path) -> None:
    for source_directory in {
        path.parent.resolve()
        for path in (*_FROZEN_PAIR_SOURCES.values(), *_FROZEN_MANIFEST_SOURCES.values())
    }:
        if output_directory == source_directory or output_directory.is_relative_to(
            source_directory
        ):
            raise ValueError(
                "output directory must be a new path outside every sealed source directory"
            )


if __name__ == "__main__":
    sys.exit(main())
