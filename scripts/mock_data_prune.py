"""Preview manual snapshot retention; stop every reader before --apply."""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import shutil
from pathlib import Path


def prune(root: Path, market: str, *, keep: int = 7, apply: bool = False) -> list[str]:
    if market not in {"us", "crypto"} or keep < 1:
        raise ValueError("valid market and keep >= 1 required")
    root = root.resolve(strict=True)
    with (root / f".{market}-refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        generations = root / f".{market}-snapshots"
        current = root / market
        if generations.is_symlink() or not current.is_symlink():
            raise ValueError("producer snapshot layout required")
        pinned = current.resolve(strict=True)
        if pinned.parent != generations or not re.fullmatch(
            r"[0-9a-f]{64}", pinned.name
        ):
            raise ValueError("current generation outside snapshot directory")
        snapshots = []
        for path in generations.iterdir():
            if not re.fullmatch(r"[0-9a-f]{64}", path.name):
                continue
            if path.is_symlink() or not path.is_dir():
                raise ValueError("unexpected generation type")
            meta = json.loads((path / "bars.json").read_text())
            if (
                meta.get("producer") != "mock-data-refresh/v1"
                or meta.get("market") != market
            ):
                raise ValueError("unexpected generation manifest")
            snapshots.append((meta["fetched_at"], path.name, path))
        # Always protect the current target, even if it is an older replay.
        archives = [x for x in sorted(snapshots, reverse=True) if x[2] != pinned]
        removals = [x[2] for x in archives[keep - 1 :]]
        if apply:
            for path in removals:
                shutil.rmtree(path)
        return [p.name for p in removals]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--market", required=True, choices=("us", "crypto"))
    parser.add_argument(
        "--keep",
        type=int,
        default=7,
        help="total retained generations including current",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete previewed generations; all readers must be stopped",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "apply": args.apply,
                "remove": prune(
                    args.root, args.market, keep=args.keep, apply=args.apply
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
