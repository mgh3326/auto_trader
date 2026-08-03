"""CLI entry point for the signed, public-only corpus job."""

from __future__ import annotations

import argparse

from .builder import CorpusBuilder
from .labeling import label_existing_exploration_parquet


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build crypto-corpus-v1 public OHLCV artifacts"
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--run",
        action="store_true",
        help="after a passing preflight gate, run/resume collection (otherwise preflight only)",
    )
    operation.add_argument(
        "--label-existing-dataset",
        action="store_true",
        help=(
            "publish value-equivalent, policy-labeled copies of exploration "
            "Parquet files without reading holdout"
        ),
    )
    args = parser.parse_args()
    if args.label_existing_dataset:
        result = label_existing_exploration_parquet()
        print(result.receipt_relative_path)
        return 0
    result = CorpusBuilder().run(preflight_only=not args.run)
    if result is None:
        print("preflight passed; historical collection was not requested")
    else:
        print(result.relative_path)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI dispatch
    raise SystemExit(main())
