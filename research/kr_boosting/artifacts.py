"""Pickled run artifacts, checked against a hash recorded when each was written.

``load_artifact`` hashes the bytes it is about to unpickle and refuses a mismatch,
so a changed or foreign file never executes. This detects a changed artifact. It
is not authentication: whoever can rewrite both an artifact and ``run_manifest.json``
passes the check, so ``--root`` must stay a directory the operator owns.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
from pathlib import Path
from typing import Any

from research.kr_boosting.panel import sha256_file

MANIFEST_NAME = "run_manifest.json"


class ArtifactRefused(ValueError):
    """An artifact has no recorded hash, or its bytes do not match it."""


def _manifest(root: Path) -> dict[str, str]:
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text())
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ArtifactRefused(f"{MANIFEST_NAME} is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ArtifactRefused(f"{MANIFEST_NAME} is not an object")
    return manifest


def write_artifact(root: Path, name: str, payload: Any) -> str:
    """Pickle ``payload`` under ``root``, record its hash and return the hash."""

    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    with path.open("wb") as handle:
        pickle.dump(payload, handle)
    digest = sha256_file(path)
    manifest = _manifest(root)
    manifest[name] = digest
    staged = root / f"{MANIFEST_NAME}.tmp"
    staged.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    os.replace(staged, root / MANIFEST_NAME)
    return digest


def _require_recorded(root: Path, name: str, data: bytes) -> None:
    recorded = _manifest(root).get(name)
    if recorded is None:
        raise ArtifactRefused(f"{name} has no hash in {MANIFEST_NAME}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != recorded:
        raise ArtifactRefused(f"{name} sha256 {actual} != recorded {recorded}")


def verify_artifact(root: Path, name: str) -> None:
    _require_recorded(root, name, (root / name).read_bytes())


def load_artifact(root: Path, name: str) -> Any:
    """Unpickle the same bytes that were checked; nothing runs before the check."""

    data = (root / name).read_bytes()
    _require_recorded(root, name, data)
    return pickle.loads(data)  # noqa: S301
