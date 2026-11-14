"""Immutable partition publication and causal lookup."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def publish(root, run_id, facts, derived, metadata):
    root = Path(root)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("invalid run ID")
    directory = root / "partitions" / run_id
    # A run ID never contains '.', so the staging path can never collide with
    # a published partition name.
    staging = directory.with_name(run_id + ".staging")
    manifest = root / "manifest.jsonl"
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

    def append_manifest():
        with manifest.open("a", encoding="utf-8") as stream:
            stream.write(encode(entry) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def manifest_has_run():
        if not manifest.exists():
            return False
        return any(
            json.loads(line).get("run_id") == run_id
            for line in manifest.read_text().splitlines()
        )

    if directory.exists():
        # A previous publish may have died after the atomic rename but before
        # the manifest append, leaving a complete partition with no record.
        # If its bytes match the payloads we would publish, adopt the orphan
        # and complete only the missing manifest entry; anything else is an
        # unknown partition and stays write-once refused.
        if not manifest_has_run() and all(
            path.is_file() and path.read_bytes() == data
            for path, data in zip(published, payloads, strict=True)
        ):
            append_manifest()
            return entry
        raise FileExistsError("partition already exists")
    # A retry of the same run ID after a mid-publish failure clears the
    # leftover staging tree; write-once applies only to published partitions.
    if staging.exists():
        shutil.rmtree(staging)
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
    append_manifest()
    return entry


def as_of(root, symbol, ts):
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("timestamp must have an offset")
    root = Path(root)
    records = [
        json.loads(line) for line in (root / "manifest.jsonl").read_text().splitlines()
    ]
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
