"""Manual collector and builder."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .build import build_rows
from .collector import Collector
from .store import publish


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Collect conservative point-in-time DART history"
    )
    parser.add_argument("command", choices=("fetch", "build"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--key-env", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--budget", type=int, default=1000)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    repo = Path(__file__).resolve().parents[2]
    if root == repo or repo in root.parents:
        parser.error("artifact root must be outside the repository")
    try:
        # Build never needs the key; it is accepted for a consistent CLI.
        key = (
            os.environ.get(args.key_env, "")
            if args.command == "fetch"
            else "build-only"
        )
        collector = Collector(root, args.run_id, args.start, args.end, key, args.budget)
        result_path = collector.path / "result.json"
        usage_path = collector.path / "usage.jsonl"
        if args.command == "fetch":
            if result_path.exists():
                print("already complete")
                return 0
            try:
                result = collector.collect()
            finally:
                with usage_path.open("a") as stream:
                    stream.write(json.dumps({"budget_used": collector.used}) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            if result["complete"]:
                # Temp-file + os.replace: a kill mid-write must not leave a
                # truncated result.json that a rerun reads as "already complete".
                tmp = result_path.with_name(result_path.name + ".tmp")
                tmp.write_text(json.dumps(result, ensure_ascii=False))
                os.replace(tmp, result_path)
                print(f"complete; calls used: {collector.used}")
            else:
                print(
                    f"budget exhausted; calls used: {collector.used}; resume with the same run ID"
                )
            return 0
        if not result_path.exists():
            raise ValueError("fetch is incomplete")
        result = json.loads(result_path.read_text())
        facts, derived = build_rows(result["reports"])
        usage = sum(
            json.loads(line)["budget_used"]
            for line in usage_path.read_text().splitlines()
        )
        end = datetime.combine(
            date.fromisoformat(args.end), time.max, ZoneInfo("Asia/Seoul")
        ).isoformat()
        entry = publish(
            root,
            args.run_id,
            facts,
            derived,
            {
                "coverage_start": args.start,
                "coverage_end": end,
                "fetch_budget_used": usage,
                "missing_reports": result["missing"],
            },
        )
        print(
            f"published facts={entry['partitions']['facts']['rows']} derived={entry['partitions']['derived']['rows']}"
        )
        return 0
    except Exception:
        print("operation failed; check arguments, cache and OpenDART availability")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
