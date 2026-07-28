#!/usr/bin/env python3
"""Verify or append the local KR-B1 P0 JSONL journal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.services.krb1_p0_journal import (
    JournalError,
    append_journal_row,
    create_journal,
    verify_journal,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAFFOLD_JOURNAL = REPO_ROOT / "docs/research/krb1/p0-anchor-ledger.initial.jsonl"
DEFAULT_JOURNAL = REPO_ROOT / "var/research/krb1/p0-anchor-ledger.jsonl"


def _object_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JournalError(f"cannot load row file {path}: {exc}") from exc
    if type(value) is not dict:
        raise JournalError("row file must contain one JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append-only KR-B1 P0 journal operator utility"
    )
    parser.add_argument(
        "--journal",
        type=Path,
        default=DEFAULT_JOURNAL,
        help="runtime JSONL journal path (default: var/research/krb1/...)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "init",
        help="exclusively create the runtime journal from the sealed scaffold",
    )
    subparsers.add_parser("verify", help="verify the complete hash-chain")
    append_parser = subparsers.add_parser("append", help="append one JSON row")
    append_parser.add_argument("--row-file", type=Path, required=True)
    args = parser.parse_args()

    try:
        if args.command == "init":
            scaffold_head = verify_journal(SCAFFOLD_JOURNAL)
            if scaffold_head.row_count != 1:
                raise JournalError("sealed scaffold must contain exactly one row")
            initial_entry = json.loads(SCAFFOLD_JOURNAL.read_text(encoding="utf-8"))
            args.journal.parent.mkdir(parents=True, exist_ok=True)
            created = create_journal(args.journal, initial_entry["row"])
            if created.as_dict() != initial_entry:
                raise JournalError("runtime initial row differs from sealed scaffold")
            print(
                json.dumps(
                    {
                        "chain_hash": created.chain_hash,
                        "journal": str(args.journal),
                        "row_count": 1,
                        "status": "INITIALIZED",
                    },
                    sort_keys=True,
                )
            )
            return 0

        if args.command == "verify":
            head = verify_journal(args.journal)
            print(
                json.dumps(
                    {
                        "chain_hash": head.chain_hash,
                        "journal": str(args.journal),
                        "row_count": head.row_count,
                        "status": "PASS",
                    },
                    sort_keys=True,
                )
            )
            return 0

        row = _object_file(args.row_file)
        entry = append_journal_row(args.journal, row)
        print(json.dumps(entry.as_dict(), sort_keys=True, ensure_ascii=False))
        return 0
    except JournalError as exc:
        parser.exit(1, f"FAIL: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
