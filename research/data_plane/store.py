"""Write-once dated partitions and the append-only manifest.

Layout under the artifact root (outside git)::

    <root>/<dataset>/<market>/date=YYYY-MM-DD/rows.jsonl.gz   write-once
    <root>/<dataset>/<market>/manifest.jsonl                  append-only
    <root>/<dataset>/<market>/.lock                           flock, never removed

A partition file is staged, fsynced and published with ``os.link`` (which
fails if the target exists), so nothing can overwrite a published partition.
Bytes are deterministic (sorted canonical JSON lines, gzip mtime 0): the same
rows always produce the same sha256. The manifest gets exactly one record per
partition; a file left without its record by a crash is recorded by the next
run (``manifest_repaired``) after its rows pass the contract again.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from research.us_universe.store import (
    AlreadyExistsError,
    append_jsonl,
    exclusive_lock,
    read_jsonl,
    write_once,
)

from .contract import CONTRACT_VERSION, ROW_KEY, canonical_json, validate_rows

ROWS_NAME = "rows.jsonl.gz"
MANIFEST_NAME = "manifest.jsonl"
LOCK_NAME = ".lock"
MANIFEST_SCHEMA = "data_plane_manifest.v1"


class ManifestMismatchError(RuntimeError):
    """A published partition does not match its manifest record."""


@dataclass(frozen=True)
class PublishResult:
    dataset: str
    market: str
    partition_date: date
    action: str  # written | already_present | manifest_repaired
    path: Path
    rows: int
    sha256: str


def dataset_dir(root: Path, dataset: str, market: str) -> Path:
    return root / dataset / market


def partition_path(root: Path, dataset: str, market: str, day: date) -> Path:
    return dataset_dir(root, dataset, market) / f"date={day.isoformat()}" / ROWS_NAME


def manifest_path(root: Path, dataset: str, market: str) -> Path:
    return dataset_dir(root, dataset, market) / MANIFEST_NAME


@contextlib.contextmanager
def dataset_lock(root: Path, dataset: str, market: str) -> Iterator[None]:
    with exclusive_lock(dataset_dir(root, dataset, market) / LOCK_NAME):
        yield


def encode_rows(dataset: str, rows: Sequence[Mapping[str, Any]]) -> bytes:
    key_fields = ROW_KEY[dataset]
    ordered = sorted(rows, key=lambda r: tuple(str(r[f]) for f in key_fields))
    text = "".join(canonical_json(r) + "\n" for r in ordered)
    return gzip.compress(text.encode("utf-8"), mtime=0)


def decode_rows(raw: bytes) -> list[dict[str, Any]]:
    text = gzip.decompress(raw).decode("utf-8")
    return [json.loads(line) for line in text.split("\n") if line]


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def manifest_records(
    root: Path, dataset: str, market: str
) -> dict[str, dict[str, Any]]:
    """Partition date -> its (first) manifest record."""
    out: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl(manifest_path(root, dataset, market)):
        day = rec.get("partition_date")
        if isinstance(day, str) and day not in out:
            out[day] = rec
    return out


def has_partition(root: Path, dataset: str, market: str, day: date) -> bool:
    return (
        day.isoformat() in manifest_records(root, dataset, market)
        and partition_path(root, dataset, market, day).exists()
    )


def read_partition(
    root: Path, dataset: str, market: str, day: date
) -> list[dict[str, Any]]:
    """Rows of a recorded partition; the bytes must match the manifest sha256."""
    rec = manifest_records(root, dataset, market).get(day.isoformat())
    if rec is None:
        raise ManifestMismatchError(f"{dataset}/{market} {day}: no manifest record")
    raw = partition_path(root, dataset, market, day).read_bytes()
    if sha256_hex(raw) != rec.get("sha256"):
        raise ManifestMismatchError(
            f"{dataset}/{market} {day}: sha256 differs from manifest"
        )
    return decode_rows(raw)


def recorded_dates(root: Path, dataset: str, market: str) -> list[date]:
    days = []
    for key in manifest_records(root, dataset, market):
        try:
            days.append(date.fromisoformat(key))
        except ValueError:
            continue
    return sorted(days)


def meta_path(root: Path, dataset: str, market: str, day: date, sha256: str) -> Path:
    """Write-once sidecar holding the run metadata of the rows with ``sha256``.

    Published *before* the rows, so a crash between the rows and the manifest
    append can be repaired without losing coverage or selection metadata.
    """
    return partition_path(root, dataset, market, day).with_name(f"meta-{sha256}.json")


def _decode_checked(raw: bytes, label: str) -> list[dict[str, Any]]:
    try:
        return decode_rows(raw)
    except (OSError, EOFError, UnicodeDecodeError, ValueError) as exc:
        raise ManifestMismatchError(
            f"{label}: published file is not readable ({type(exc).__name__})"
        ) from exc


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def publish_partition(
    root: Path,
    *,
    dataset: str,
    market: str,
    day: date,
    rows: Sequence[Mapping[str, Any]],
    meta: Mapping[str, Any],
) -> PublishResult:
    """Validate and publish one partition. Caller must hold ``dataset_lock``.

    If the partition is already published and recorded, nothing is written
    (after checking its bytes against the manifest). If it is published but
    unrecorded (crash between link and append), it is recorded from the
    on-disk rows and their metadata sidecar; the new ``rows`` are ignored, and
    a partition whose sidecar is missing is refused rather than recorded
    without its coverage metadata.
    """
    label = f"{dataset}/{market} {day}"
    path = partition_path(root, dataset, market, day)
    records = manifest_records(root, dataset, market)
    if path.exists():
        raw = path.read_bytes()
        digest = sha256_hex(raw)
        if day.isoformat() in records:
            if digest != records[day.isoformat()].get("sha256"):
                raise ManifestMismatchError(
                    f"{label}: published file differs from its manifest sha256 "
                    f"(corrupted or replaced); not reported as present"
                )
            return PublishResult(
                dataset,
                market,
                day,
                "already_present",
                path,
                records[day.isoformat()].get("rows", -1),
                digest,
            )
        on_disk = validate_rows(market, dataset, day, _decode_checked(raw, label))
        saved = _read_meta(meta_path(root, dataset, market, day, digest))
        if saved is None:
            raise ManifestMismatchError(
                f"{label}: unrecorded partition without its metadata sidecar; "
                f"refusing to record it without coverage/selection metadata"
            )
        _append_record(
            root, dataset, market, day, raw, on_disk, {**saved, "repaired": True}
        )
        return PublishResult(
            dataset, market, day, "manifest_repaired", path, len(on_disk), digest
        )
    checked = validate_rows(market, dataset, day, rows)
    raw = encode_rows(dataset, checked)
    sidecar = meta_path(root, dataset, market, day, sha256_hex(raw))
    try:
        write_once(sidecar, (canonical_json(meta) + "\n").encode("utf-8"))
    except AlreadyExistsError:
        pass  # identical rows from an attempt that died before linking them
    try:
        write_once(path, raw)
    except AlreadyExistsError:
        # Another process published between our check and link; theirs wins.
        raw = path.read_bytes()
        checked = validate_rows(market, dataset, day, _decode_checked(raw, label))
        sidecar = meta_path(root, dataset, market, day, sha256_hex(raw))
    if day.isoformat() not in manifest_records(root, dataset, market):
        _append_record(
            root, dataset, market, day, raw, checked, _read_meta(sidecar) or dict(meta)
        )
    return PublishResult(
        dataset, market, day, "written", path, len(checked), sha256_hex(raw)
    )


def _append_record(
    root: Path,
    dataset: str,
    market: str,
    day: date,
    raw: bytes,
    rows: Sequence[Mapping[str, Any]],
    meta: Mapping[str, Any],
) -> None:
    observed = sorted(r["observed_at"] for r in rows)
    record = {
        "schema": MANIFEST_SCHEMA,
        "contract": CONTRACT_VERSION,
        "dataset": dataset,
        "market": market,
        "partition_date": day.isoformat(),
        "path": partition_path(Path("."), dataset, market, day).as_posix(),
        "sha256": sha256_hex(raw),
        "rows": len(rows),
        "observed_at_min": observed[0] if observed else None,
        "observed_at_max": observed[-1] if observed else None,
        **dict(meta),
    }
    append_jsonl(manifest_path(root, dataset, market), record)
