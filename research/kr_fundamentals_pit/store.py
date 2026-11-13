"""Immutable partition publication and causal lookup."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def publish(root, run_id, facts, derived, metadata):
    root = Path(root)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("invalid run ID")
    directory = root / "partitions" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.jsonl"
    paths = [
        directory / "facts.jsonl",
        directory / "derived.jsonl",
        directory / "metadata.json",
    ]
    if any(p.exists() for p in paths):
        raise FileExistsError("partition already exists")
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
                ("facts", "derived"), paths, payloads, (facts, derived), strict=False
            )
        },
    }
    payloads.append(encode(entry).encode())
    for path, data in zip(paths, payloads, strict=True):
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
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
    return [versions[key][1] for key in sorted(versions)]
