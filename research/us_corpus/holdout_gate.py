"""The only sanctioned path to the sealed holdout — write in, nothing out.

R1 claimed `written_not_read: true` while a post-hoc `rglob` checksum sweep had
in fact opened and read both sealed partitions to hash them. The values were
never looked at by a human or a model, so the analysis was not contaminated —
but the record said "not read" and the file *had* been read, and the access log
could not have caught it because it only ever recorded writes. A log that can
only record the thing you want to be true is not evidence.

This module fixes the shape of the problem rather than the instance:

* digests are taken from the write buffer (`labeling.write_labeled_parquet`),
  so nothing ever needs to reopen a sealed file,
* `guard_read` is a real refusal path that **logs a READ line and raises**, so
  the access log has a code path that can record a read,
* `assert_no_unguarded_holdout_access` statically pins that no other module in
  this package touches `HOLDOUT_DIR`, and that the deleted `rglob` sweep has not
  come back.

🔴 There is deliberately no read function here. Adding one is the thing this
file exists to make difficult.

Path comparison is `resolve()` + case-fold because macOS filesystems are
case-insensitive: `HOLDOUT/`, `Holdout/` and `holdout/` are the same directory
and a case-sensitive check would wave two of them through.
"""

from __future__ import annotations

import ast
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from research.us_corpus import config as cfg
from research.us_corpus.labeling import WriteReceipt, write_labeled_parquet

__all__ = [
    "HoldoutReadRefused",
    "assert_no_unguarded_holdout_access",
    "guard_read",
    "is_under_holdout",
    "log_access",
    "write_partition",
]


class HoldoutReadRefused(RuntimeError):
    """A read of the sealed holdout was attempted and refused."""


def is_under_holdout(path: Path, holdout_dir: Path | None = None) -> bool:
    root = (holdout_dir or cfg.HOLDOUT_DIR).resolve()
    try:
        candidate = Path(path).resolve()
    except OSError:
        candidate = Path(path).absolute()
    root_parts = [p.casefold() for p in root.parts]
    cand_parts = [p.casefold() for p in candidate.parts]
    return cand_parts[: len(root_parts)] == root_parts


def log_access(kind: str, message: str, log_path: Path | None = None) -> None:
    """Append-only ledger. `kind` is WRITE or READ — both are recordable."""
    target = log_path or cfg.HOLDOUT_ACCESS_LOG
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        # The ledger carries row counts and digests, so it is a numeric artifact
        # and gets the label like every other one.
        target.write_text(
            f"# SURVIVORSHIP_BIASED=TRUE corpus={cfg.CORPUS_ID}\n"
            "# columns: utc_timestamp\\tkind(WRITE|READ)\\tdetail\n",
            encoding="utf-8",
        )
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}\t{kind}\t{message}\n")
        handle.flush()
        os.fsync(handle.fileno())


def guard_read(
    path: Path,
    reason: str,
    holdout_dir: Path | None = None,
    log_path: Path | None = None,
) -> Path:
    """Refuse (and record) any read that resolves under the holdout root.

    🔴 The refusal is an exception. Returning an empty frame instead would let
    the caller report a clean run over data it never actually saw.
    """
    if is_under_holdout(path, holdout_dir):
        log_access("READ", f"REFUSED {path} :: {reason}", log_path)
        raise HoldoutReadRefused(
            f"refusing to read sealed holdout path {path} ({reason}). "
            "The holdout is write-only until forward OOS begins."
        )
    return Path(path)


def write_partition(frame: pd.DataFrame, target: Path) -> WriteReceipt:
    """Write one sealed partition and record the write-time digest.

    The digest is computed from the serialised buffer, so proving integrity
    later never requires reopening this file.
    """
    if not is_under_holdout(target):
        raise ValueError(f"{target} is not under the holdout root")
    receipt = write_labeled_parquet(frame, target)
    log_access(
        "WRITE",
        f"{receipt.relative_path} rows={receipt.row_count} "
        f"bytes={receipt.bytes_written} sha256={receipt.sha256}",
    )
    return receipt


def assert_no_unguarded_holdout_access(package_dir: Path | None = None) -> list[str]:
    """Static proof that nothing in this package can read the sealed holdout.

    Returns offending locations; empty means the invariant holds. The scan is
    AST-based on purpose: a text scan flags its own docstrings and the guard's
    own literals, which produces noise that trains a reader to ignore it.

    Two properties are pinned:

    1. **No artifact-root sweep.** `ARTIFACT_ROOT.rglob(...)` / `.glob(...)` is
       exactly the construct that hashed the sealed files in R1. Writing a
       holdout exclusion into such a sweep is not accepted here — the sweep
       itself is refused, because an exclusion is one edit away from being
       wrong again.
    2. **No read call receives a holdout path.** Any call to a known read
       function whose arguments mention `HOLDOUT_DIR` is an offence, regardless
       of which module it lives in. Naming `HOLDOUT_DIR` to *write* to it, or
       to print it into a manifest, is fine and is not flagged.
    """
    read_funcs = {
        "open",
        "read_text",
        "read_bytes",
        "read_parquet",
        "read_table",
        "read_schema",
        "read_csv",
        "sha256_file",
        "verify_label",
        "read_labeled_parquet",
        "glob",
        "rglob",
        "iterdir",
        "walk",
    }
    root = package_dir or Path(__file__).resolve().parent
    offenders: list[str] = []

    for source in sorted(root.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )

            if name in {"glob", "rglob"} and isinstance(func, ast.Attribute):
                receiver = ast.unparse(func.value)
                if "ARTIFACT_ROOT" in receiver:
                    offenders.append(
                        f"{source.name}:{node.lineno}: artifact-root sweep "
                        f"reintroduced :: {receiver}.{name}(...)"
                    )

            if name in read_funcs:
                args = " ".join(ast.unparse(a) for a in node.args)
                receiver = (
                    ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""
                )
                if "HOLDOUT_DIR" in args or "HOLDOUT_DIR" in receiver:
                    offenders.append(
                        f"{source.name}:{node.lineno}: read call on holdout "
                        f":: {name}({args})"
                    )
    return offenders
