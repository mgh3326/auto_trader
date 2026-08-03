"""Survivorship labelling, write-time digests, and a refusing loader.

Two problems are solved together here because they share one mechanism.

**Labelling.** The corpus is survivorship-biased. A label that lives only in
`manifest.json` is a label a consumer never has to read. So the label is stamped
into the Parquet schema metadata of every partition and into every artifact that
carries numbers, and the supported loader *refuses* to return rows from a file
that lacks it.

**Digests.** Every digest in this package is computed from the bytes at the
moment they are written — never by re-opening the file afterwards. That is not
an optimisation: a post-hoc `rglob` hashing sweep is exactly how the sealed
holdout got read while the manifest claimed it had not been. Removing the read
removes the possibility, rather than relying on an exclusion list being correct.

🔴 The refusal is an exception, never a silent filter. A filtered-to-empty
result is indistinguishable from a successful read of an empty partition, which
is how a guard becomes decorative.
"""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from research.us_corpus import config as cfg

LABEL_KEY = b"SURVIVORSHIP_BIASED"
LABEL_VALUE = b"TRUE"

# Mirrors of the label for text artifacts (CSV column / JSON field).
LABEL_FIELD = "survivorship_biased"
LABEL_FIELD_VALUE = "TRUE"

SURVIVORSHIP_NOTE = (
    "Universe is a frozen snapshot of CURRENTLY ACTIVE US common stocks. "
    "Symbols delisted before the snapshot are absent entirely, so any return, "
    "hit-rate or drawdown computed from this corpus is biased OPTIMISTIC. "
    "Never cite these numbers without this label. The KR corpus resolved this "
    "via pykrx delisting history; the US corpus could not, so the two are not "
    "directly comparable on any performance metric."
)


class UnlabeledCorpusError(RuntimeError):
    """A Parquet file carried no survivorship label, so the read is refused."""


@dataclass(frozen=True)
class WriteReceipt:
    """Proof of a write: digest of the exact bytes emitted, and their count."""

    relative_path: str
    sha256: str
    bytes_written: int
    row_count: int


def label_metadata() -> dict[bytes, bytes]:
    return {
        LABEL_KEY: LABEL_VALUE,
        b"corpus_id": cfg.CORPUS_ID.encode(),
        b"purpose": cfg.PURPOSE.encode(),
        b"survivorship_note": SURVIVORSHIP_NOTE.encode(),
        b"price_mode": cfg.PRICE_MODE.encode(),
        b"session_calendar": cfg.SESSION_CALENDAR.encode(),
    }


def label_fields() -> dict[str, str]:
    """Label block for JSON artifacts."""
    return {
        LABEL_FIELD: LABEL_FIELD_VALUE,
        "corpus_id": cfg.CORPUS_ID,
        "purpose": cfg.PURPOSE,
        "survivorship_note": SURVIVORSHIP_NOTE,
    }


def _atomic_write(target: Path, payload: bytes) -> None:
    """`.partial` -> fsync -> rename. The finished file is never reopened."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    with partial.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, target)


def write_labeled_parquet(
    frame: pd.DataFrame, target: Path, root: Path | None = None
) -> WriteReceipt:
    """Serialise, label, hash, then write. 🔴 The digest comes from the buffer.

    Because the bytes are hashed before they reach the filesystem, no digest in
    this package ever requires opening a written file — including a sealed one.
    """
    table = pa.Table.from_pandas(frame, preserve_index=False)
    merged = {**(table.schema.metadata or {}), **label_metadata()}
    table = table.replace_schema_metadata(merged)

    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="zstd")
    payload = buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()

    _atomic_write(target, payload)
    base = root or cfg.ARTIFACT_ROOT
    return WriteReceipt(
        relative_path=str(target.relative_to(base)),
        sha256=digest,
        bytes_written=len(payload),
        row_count=int(len(frame)),
    )


def write_labeled_bytes(
    target: Path, payload: bytes, rows: int = 0, root: Path | None = None
) -> WriteReceipt:
    """Same write-time-digest contract for non-Parquet artifacts."""
    digest = hashlib.sha256(payload).hexdigest()
    _atomic_write(target, payload)
    base = root or cfg.ARTIFACT_ROOT
    return WriteReceipt(
        relative_path=str(target.relative_to(base)),
        sha256=digest,
        bytes_written=len(payload),
        row_count=rows,
    )


def write_labeled_csv(
    frame: pd.DataFrame, target: Path, root: Path | None = None
) -> WriteReceipt:
    """Every numeric CSV carries the label as a column.

    A per-row column is redundant, and that is the point: it survives being
    opened in a spreadsheet, sliced, or re-exported, whereas a header comment
    does not and would also break naive parsers.
    """
    labeled = frame.copy()
    labeled.insert(0, LABEL_FIELD, LABEL_FIELD_VALUE)
    payload = labeled.to_csv(index=False).encode("utf-8")
    return write_labeled_bytes(target, payload, rows=len(frame), root=root)


def read_labeled_parquet(path: Path) -> pd.DataFrame:
    """🔴 Supported read path. Refuses an unlabelled file with an exception.

    This is the guard the brief requires. It does not filter, warn, or return an
    empty frame — any of those would let a consumer proceed unaware.
    """
    table = pq.read_table(path)
    metadata = table.schema.metadata or {}
    if metadata.get(LABEL_KEY) != LABEL_VALUE:
        raise UnlabeledCorpusError(
            f"{path} carries no {LABEL_KEY.decode()}={LABEL_VALUE.decode()} "
            "metadata. Refusing to return rows: numbers derived from this "
            "corpus must never be cited without the survivorship label. "
            f"{SURVIVORSHIP_NOTE}"
        )
    return table.to_pandas()


def verify_label(path: Path) -> bool:
    """Metadata-only check used by the audit sweep (does not return rows)."""
    metadata = pq.read_schema(path).metadata or {}
    return metadata.get(LABEL_KEY) == LABEL_VALUE
