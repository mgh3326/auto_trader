"""Write CSV and Parquet into a staging directory, verify the bytes, then publish.

Nothing reaches the output directory unless the leak check passed on the rows
and again on the files read back. A failed run leaves no partial output.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.services.dots_history_export.leak_check import (
    LEAK_RULE_NAMES,
    LeakCheckFailed,
    Violation,
    scan_table,
)
from app.services.dots_history_export.projection import OutputRow, Projection, Window
from app.services.dots_history_export.spec import (
    SCHEMA_VERSION,
    SOURCES_WITHOUT_LEDGER,
    TABLES,
    Column,
    Kind,
    TableSpec,
)

MANIFEST_NAME = "manifest.json"
_DECIMAL_TYPE = pa.decimal128(38, 12)
_UTC_TYPE = pa.timestamp("us", tz="UTC")


class OutputDirectoryError(ValueError):
    """The output directory cannot be used; nothing was written."""


def _arrow_type(column: Column) -> pa.DataType:
    if column.kind is Kind.DECIMAL:
        return _DECIMAL_TYPE
    if column.kind is Kind.INTEGER:
        return pa.int64()
    if column.kind is Kind.BOOLEAN:
        return pa.bool_()
    if column.kind is Kind.TS_UTC:
        return _UTC_TYPE
    if column.kind is Kind.DATE:
        return pa.date32()
    return pa.string()


def _arrow_value(column: Column, value: str | None) -> Any:
    if value is None or value == "":
        return None
    if column.kind is Kind.DECIMAL:
        return Decimal(value)
    if column.kind is Kind.INTEGER:
        return int(value)
    if column.kind is Kind.BOOLEAN:
        return value == "true"
    if column.kind is Kind.TS_UTC:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if column.kind is Kind.DATE:
        return date.fromisoformat(value)
    return value


def _cell_from_arrow(column: Column, value: Any) -> str | None:
    if value is None:
        return None
    if column.kind is Kind.DECIMAL:
        return format(value, "f")
    if column.kind is Kind.BOOLEAN:
        return "true" if value else "false"
    if column.kind is Kind.TS_UTC:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if column.kind is Kind.DATE:
        return value.isoformat()
    return str(value)


def _write_csv(path: Path, spec: TableSpec, rows: list[OutputRow]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(spec.column_names)
        for row in rows:
            writer.writerow([row.get(name) or "" for name in spec.column_names])


def _write_parquet(path: Path, spec: TableSpec, rows: list[OutputRow]) -> None:
    arrays = [
        pa.array(
            [_arrow_value(column, row.get(column.name)) for row in rows],
            type=_arrow_type(column),
        )
        for column in spec.columns
    ]
    pq.write_table(pa.Table.from_arrays(arrays, names=list(spec.column_names)), path)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str | None]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
        return list(reader.fieldnames or []), rows


def _read_parquet(
    path: Path, spec: TableSpec
) -> tuple[list[str], list[dict[str, str | None]]]:
    table = pq.read_table(path)
    by_name = {column.name: column for column in spec.columns}
    rows = []
    for raw in table.to_pylist():
        rows.append(
            {
                name: _cell_from_arrow(by_name[name], value)
                if name in by_name
                else str(value)
                for name, value in raw.items()
            }
        )
    return list(table.column_names), rows


def check_written_files(
    directory: Path,
    expected_rows: Mapping[str, int],
    *,
    forbidden: Collection[str],
    salt: bytes,
) -> list[Violation]:
    """The final gate: re-read every published byte instead of trusting the writer."""
    violations: list[Violation] = []
    expected_files = {
        f"{spec.name}.{suffix}" for spec in TABLES for suffix in ("csv", "parquet")
    }
    for path in sorted(directory.iterdir()):
        if path.name not in expected_files and path.name != MANIFEST_NAME:
            violations.append(Violation(path.name, "", 0, "file_not_allowlisted"))
    for spec in TABLES:
        for suffix in ("csv", "parquet"):
            path = directory / f"{spec.name}.{suffix}"
            name = f"{spec.name}.{suffix}"
            columns, rows = (
                _read_csv(path) if suffix == "csv" else _read_parquet(path, spec)
            )
            found = scan_table(spec, columns, rows, forbidden=forbidden)
            violations.extend(
                Violation(name, item.column, item.row, item.rule) for item in found
            )
            if len(rows) != expected_rows[spec.name]:
                violations.append(Violation(name, "", 0, "row_count_mismatch"))
            if suffix == "csv" and salt in path.read_bytes():
                violations.append(Violation(name, "", 0, "salt_in_output"))
    return violations


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_entry(path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def build_manifest(
    directory: Path,
    *,
    projection: Projection,
    window: Window,
    code_commit: str,
    code_dirty: bool | None,
    salt_fingerprint: str,
    generated_at: datetime,
) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for spec in TABLES:
        tables[spec.name] = {
            "row_count": len(projection.tables[spec.name]),
            "columns": list(spec.column_names),
            "window_column": spec.window_column,
            "tags_hashed": projection.tags_hashed.get(spec.name, 0),
            "files": {
                suffix: _file_entry(directory / f"{spec.name}.{suffix}")
                for suffix in ("csv", "parquet")
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": generated_at.astimezone(UTC).isoformat(),
        "window": {
            "since_utc": window.since.astimezone(UTC).isoformat(),
            "until_utc": window.until.astimezone(UTC).isoformat(),
            "end": "exclusive",
        },
        "code_commit": code_commit,
        "code_dirty": code_dirty,
        "salt_fingerprint": salt_fingerprint,
        "leak_check": {
            "status": "passed",
            "rules": list(LEAK_RULE_NAMES),
            "forbidden_value_count": len(projection.forbidden),
        },
        "sources_without_ledger": SOURCES_WITHOUT_LEDGER,
        "tables": tables,
    }


def resolve_output_directory(out_dir: Path, *, repo_root: Path) -> Path:
    resolved = out_dir.expanduser().resolve()
    if resolved.is_relative_to(repo_root.resolve()):
        raise OutputDirectoryError("--out must be outside the repository")
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise OutputDirectoryError("--out must not exist or must be an empty directory")
    if not resolved.parent.is_dir():
        raise OutputDirectoryError("the parent of --out must already exist")
    return resolved


def publish(
    projection: Projection,
    *,
    out_dir: Path,
    window: Window,
    salt: bytes,
    salt_fingerprint: str,
    code_commit: str,
    code_dirty: bool | None,
    extra_forbidden: Collection[str] = (),
    generated_at: datetime | None = None,
    write_files: Callable[[Path, Projection], None] | None = None,
) -> dict[str, Any]:
    """Validate, write, re-validate, then move the finished directory into place."""
    forbidden = {
        *projection.forbidden,
        *extra_forbidden,
        salt.decode("utf-8", errors="ignore"),
    }
    violations: list[Violation] = []
    for spec in TABLES:
        rows = projection.tables[spec.name]
        violations.extend(
            scan_table(spec, spec.column_names, rows, forbidden=forbidden)
        )
    if violations:
        raise LeakCheckFailed(violations)

    staging = Path(
        tempfile.mkdtemp(prefix=f".{out_dir.name}.staging-", dir=out_dir.parent)
    )
    try:
        (write_files or _write_all)(staging, projection)
        violations = check_written_files(
            staging,
            {spec.name: len(projection.tables[spec.name]) for spec in TABLES},
            forbidden=forbidden,
            salt=salt,
        )
        if violations:
            raise LeakCheckFailed(violations)
        manifest = build_manifest(
            staging,
            projection=projection,
            window=window,
            code_commit=code_commit,
            code_dirty=code_dirty,
            salt_fingerprint=salt_fingerprint,
            generated_at=generated_at or datetime.now(UTC),
        )
        manifest_bytes = (
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        if salt in manifest_bytes:
            raise LeakCheckFailed([Violation(MANIFEST_NAME, "", 0, "salt_in_output")])
        (staging / MANIFEST_NAME).write_bytes(manifest_bytes)
        for path in staging.iterdir():
            path.chmod(0o600)
        if out_dir.exists():
            out_dir.rmdir()
        os.rename(staging, out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _write_all(directory: Path, projection: Projection) -> None:
    for spec in TABLES:
        rows = projection.tables[spec.name]
        _write_csv(directory / f"{spec.name}.csv", spec, rows)
        _write_parquet(directory / f"{spec.name}.parquet", spec, rows)
