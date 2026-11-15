"""Immutable partition publication and causal lookup.

Crash-safety contract (design decision, hk 3793): this is a hand-run research
collector, so on any detected inconsistency it refuses and tells the operator
what to delete or repair — it never auto-repairs. See consistency_problems().
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manifest_records(manifest):
    """Parse append-only manifest lines; any unparseable line raises."""
    manifest = Path(manifest)
    if not manifest.exists():
        return []
    return [json.loads(line) for line in manifest.read_text().splitlines()]


def consistency_problems(root):
    """Detect store states a human must repair before any further write.

    Returns a list of problems, each with the manual fix. The checked states
    are exactly the crash residue of publish: leftover staging, a manifest
    torn or unparseable, a partition with no manifest entry, or a manifest
    entry with no partition.
    """
    root = Path(root)
    manifest = root / "manifest.jsonl"
    partitions = root / "partitions"
    problems = []
    records = []
    if manifest.exists():
        if manifest.stat().st_size and manifest.read_bytes()[-1:] != b"\n":
            problems.append(
                "manifest.jsonl ends mid-line (torn append): truncate it to "
                "the last newline"
            )
        for index, line in enumerate(manifest.read_text().splitlines(), 1):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                problems.append(
                    f"manifest.jsonl line {index} is not valid JSON: fix or "
                    "remove that line"
                )
    staging_dirs, partition_dirs = [], set()
    if partitions.is_dir():
        for child in sorted(partitions.iterdir()):
            if child.name.endswith(".staging"):
                staging_dirs.append(child.name)
            elif child.is_dir():
                partition_dirs.add(child.name)
    for name in staging_dirs:
        problems.append(f"leftover staging {name}: delete partitions/{name} and retry")
    recorded = {r.get("run_id") for r in records if isinstance(r, dict)} - {None}
    for name in sorted(partition_dirs - recorded):
        problems.append(
            f"partition {name} has no manifest entry: delete partitions/{name}"
        )
    for name in sorted(recorded - partition_dirs):
        problems.append(
            f"manifest entry for {name} has no partition: remove that manifest line"
        )
    return problems


def publish(root, run_id, facts, derived, metadata):
    root = Path(root)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("invalid run ID")
    problems = consistency_problems(root)
    if problems:
        raise RuntimeError(
            "artifact store is inconsistent; repair by hand before retrying:\n- "
            + "\n- ".join(problems)
        )
    directory = root / "partitions" / run_id
    # A run ID never contains '.', so the staging path can never collide with
    # a published partition name.
    staging = directory.with_name(run_id + ".staging")
    manifest = root / "manifest.jsonl"
    if directory.exists():
        raise FileExistsError("partition already exists")
    names = ("facts.jsonl", "derived.jsonl", "metadata.json")
    paths = [staging / name for name in names]
    published = [directory / name for name in names]
    payloads = [
        ("\n".join(encode(row) for row in rows) + ("\n" if rows else "")).encode()
        for rows in (facts, derived)
    ]
    entry = {
        **metadata,
        "run_id": run_id,
        "partitions": {
            name: {
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(data).hexdigest(),
                "rows": len(rows),
            }
            for name, path, data, rows in zip(
                ("facts", "derived"),
                published,
                payloads,
                (facts, derived),
                strict=False,
            )
        },
    }
    payloads.append(encode(entry).encode())
    # Write everything into staging, fsync, publish with one atomic rename,
    # then append the manifest entry last.
    staging.mkdir(parents=True)
    for path, data in zip(paths, payloads, strict=True):
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    fd = os.open(staging, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if directory.exists():
        raise FileExistsError("partition already exists")
    os.rename(staging, directory)
    # Make the new directory entry durable: without this fsync a crash can lose
    # the partition while the manifest entry survives.
    fd = os.open(directory.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    with manifest.open("a", encoding="utf-8") as stream:
        stream.write(encode(entry) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return entry


def as_of(root, symbol, ts):
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("timestamp must have an offset")
    root = Path(root)
    records = manifest_records(root / "manifest.jsonl")
    end = max(datetime.fromisoformat(record["coverage_end"]) for record in records)
    if ts > end:
        raise ValueError("lookup exceeds coverage end")
    versions = {}
    for record in records:
        part = record["partitions"]["derived"]
        data = (root / part["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != part["sha256"]:
            raise ValueError("partition hash mismatch")
        for line in data.splitlines():
            row = json.loads(line)
            available = datetime.fromisoformat(row["available_at"])
            if row["symbol"] != symbol or available > ts:
                continue
            key = (
                row["corp_code"],
                row["business_year"],
                row["report_code"],
                row["period"],
            )
            rank = (available.astimezone(UTC), row["rcept_no"])
            if key not in versions or rank > versions[key][0]:
                versions[key] = (rank, row)
    # A known newer filing with no matched statement invalidates the older
    # value from its receipt EOD, even though no replacement row was published.
    for record in records:
        for missing in record["missing_reports"]:
            fields = (
                "corp_code",
                "business_year",
                "report_code",
                "period",
                "rcept_date",
                "rcept_no",
            )
            if missing.get("symbol") != symbol or not all(
                field in missing for field in fields
            ):
                continue
            available = datetime.strptime(missing["rcept_date"], "%Y%m%d").replace(
                hour=23,
                minute=59,
                second=59,
                microsecond=999999,
                tzinfo=ZoneInfo("Asia/Seoul"),
            )
            key = tuple(missing[field] for field in fields[:4])
            rank = (available.astimezone(UTC), missing["rcept_no"])
            if available <= ts and key in versions and rank > versions[key][0]:
                versions[key] = (rank, None)
    return [
        versions[key][1] for key in sorted(versions) if versions[key][1] is not None
    ]
