"""CLI entry point for the signed, public-only corpus job."""

from __future__ import annotations

import argparse

from .builder import CorpusBuilder


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build crypto-corpus-v1 public OHLCV artifacts"
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="after a passing preflight gate, run/resume collection (otherwise preflight only)",
    )
    args = parser.parse_args()
    result = CorpusBuilder().run(preflight_only=not args.run)
    if result is None:
        print("preflight passed; historical collection was not requested")
    else:
        print(result.relative_path)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI dispatch
    raise SystemExit(main())
