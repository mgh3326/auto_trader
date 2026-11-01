"""Load the dots history export, refusing anything the manifest does not vouch for."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

SCHEMA_VERSION = "dots-history-export/v1"
MANIFEST_NAME = "manifest.json"


class ExportIntegrityError(RuntimeError):
    """The export on disk is not the export its manifest describes."""


@dataclass(frozen=True)
class Export:
    tables: dict[str, pd.DataFrame]
    manifest: dict
    manifest_sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest(root: Path) -> tuple[dict, str]:
    """Check every file against the manifest. Raises before any row is read."""
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ExportIntegrityError(f"no {MANIFEST_NAME} in {root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ExportIntegrityError(
            f"schema_version {manifest.get('schema_version')!r} is not {SCHEMA_VERSION}"
        )
    tables = manifest.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise ExportIntegrityError("manifest lists no tables")

    expected = {MANIFEST_NAME}
    for name, table in tables.items():
        for kind, spec in table["files"].items():
            expected.add(spec["name"])
            path = root / spec["name"]
            if not path.is_file():
                raise ExportIntegrityError(f"{name}: {kind} file is missing")
            if path.stat().st_size != spec["bytes"]:
                raise ExportIntegrityError(f"{name}: {kind} size mismatch")
            if sha256_file(path) != spec["sha256"]:
                raise ExportIntegrityError(f"{name}: {kind} sha256 mismatch")
    unexpected = sorted(p.name for p in root.iterdir() if p.name not in expected)
    if unexpected:
        raise ExportIntegrityError(f"files not in the manifest: {unexpected}")
    return manifest, sha256_file(manifest_path)


def load_export(root: Path) -> Export:
    root = Path(root).expanduser()
    manifest, digest = verify_manifest(root)
    tables: dict[str, pd.DataFrame] = {}
    for name, table in manifest["tables"].items():
        path = root / table["files"]["csv"]["name"]
        with path.open(encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle))
        if header != table["columns"]:
            raise ExportIntegrityError(f"{name}: columns differ from the manifest")
        # Every cell stays text so KR codes keep their leading zeros.
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        if len(frame) != table["row_count"]:
            raise ExportIntegrityError(f"{name}: row count differs from the manifest")
        tables[name] = frame
    return Export(tables=tables, manifest=manifest, manifest_sha256=digest)
