"""Manual CLI for the read-only US upside shadow-instrumentation contract.

The command reads a captured JSON snapshot and appends an operator-selected
local JSONL record. It has no source collection, broker, account, proposal,
database, or scheduler behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.services.us_upside_instrumentation import (
    InstrumentationInput,
    append_session_jsonl,
    evaluate_instrumentation,
    load_session_jsonl,
    read_three_completed_sessions,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manual US upside shadow-instrumentation recorder"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser(
        "record",
        help="Validate one captured bounded cohort and append one JSONL record.",
    )
    record.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Read-only captured bounded-cohort JSON.",
    )
    record.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Operator-selected local JSONL artifact.",
    )

    read_three = subparsers.add_parser(
        "read-three",
        help="Read exactly three completed records without changing any threshold.",
    )
    read_three.add_argument(
        "--records",
        type=Path,
        required=True,
        help="Three-line JSONL artifact produced by the record command.",
    )
    return parser.parse_args(argv)


def _load_snapshot(path: Path) -> tuple[InstrumentationInput, str]:
    raw = path.read_bytes()
    return InstrumentationInput.model_validate_json(raw), hashlib.sha256(
        raw
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "record":
        snapshot, input_hash = _load_snapshot(args.input)
        record = evaluate_instrumentation(snapshot, input_hash=input_hash)
        append_session_jsonl(args.output, record)
        print(
            json.dumps(
                {
                    "session_id": record.session_id,
                    "input_hash": record.input_hash,
                    "arm_shadow_counts": record.arm_shadow_counts,
                    "coverage_complete": record.coverage.coverage_complete,
                    "interpretation": record.interpretation.conclusion,
                    "read_only_safety": record.read_only_safety.model_dump(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    records = load_session_jsonl(args.records)
    reading = read_three_completed_sessions(records)
    print(
        json.dumps(
            reading.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
