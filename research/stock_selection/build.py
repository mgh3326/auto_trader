"""Build a session-aligned dataset from a local, hash-verified snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path

from .dataset import build_dataset
from .io import add_input_args, digest, read_bars, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_input_args(parser)
    parser.add_argument("--start", required=True)
    parser.add_argument(
        "--end",
        required=True,
        help="inclusive; load warmup and label-tail bars from cache",
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    bars, meta = read_bars(args.cache_dir, args.market)
    dataset = build_dataset(bars, args.market, liquid_n=args.liquid_n)
    dataset = dataset[dataset.date.between(args.start, args.end)]
    path = (
        args.output_dir or args.cache_dir / args.market
    ) / f"dataset-{args.start}-{args.end}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_csv(path, index=False)
    write_json(
        path.with_suffix(".json"),
        {
            "input": meta,
            "sha256": digest(path),
            "rows": len(dataset),
            "eligible": int(dataset.eligible.sum()),
            "unknown_outcomes": {
                str(h): int(dataset[f"return_{h}"].isna().sum()) for h in (1, 3, 5, 10)
            },
        },
    )
    print(path)


if __name__ == "__main__":
    main()
