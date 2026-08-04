"""Survivorship-bias labelling, enforced at the data layer.

§0 pitfall 2 (the reason this module exists)
--------------------------------------------
In `us-corpus-v1` the survivorship label lived only in the first key of
`manifest.json`. It was absent from `reports/*.csv`, `reports/*.json` and from
the parquet file metadata, and no loader required it. A consumer could read
`dataset/` directly and publish a Sharpe ratio having never seen the word
"survivorship". A documentation warning is not a contract.

Here the label is enforced in three independent places, so that a consumer has
to go out of their way to *avoid* it rather than out of their way to find it:

1. `attach_parquet_metadata()`  -- every parquet carries the label in its own
   file-level key/value metadata, so `pq.ParquetFile(p).metadata.metadata`
   shows it even with no manifest present.
2. `label_fields()` / `write_labelled_csv()` / `write_labelled_json()` -- every
   artifact carrying numbers embeds the label as a real field.
3. `loader.load_dataset()` -- refuses to return a dataframe unless the caller
   passes `acknowledge_survivorship_bias=True`.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import config

SURVIVORSHIP_BIASED = "TRUE"

SURVIVORSHIP_NOTE = (
    "SURVIVORSHIP_BIASED=TRUE. The symbol universe is a frozen snapshot of "
    "US common stocks that were ACTIVE at snapshot time. Symbols delisted "
    "before the snapshot are absent entirely. Any return, hit-rate, Sharpe or "
    "drawdown computed from this corpus is biased OPTIMISTIC and must never be "
    "cited without this label. Intraday bars inherit the same universe bias as "
    "the daily sister corpus us-corpus-v1."
)

PURPOSE_NOTE = (
    "EXPLORATORY_BACKTEST_RESEARCH_ONLY. Not a production signal source. "
    "Not validated for live trading."
)

# Byte-valued kv metadata stamped into every parquet this corpus writes.
PARQUET_METADATA: dict[bytes, bytes] = {
    b"SURVIVORSHIP_BIASED": SURVIVORSHIP_BIASED.encode(),
    b"survivorship_note": SURVIVORSHIP_NOTE.encode(),
    b"corpus_id": config.CORPUS_ID.encode(),
    b"purpose": config.PURPOSE.encode(),
    b"source_product": config.SOURCE_PRODUCT.encode(),
    b"timestamp_storage": config.TIMESTAMP_STORAGE_TZ.encode(),
    b"session_date_derived_from": config.SESSION_DATE_TZ.encode(),
}


def label_fields() -> dict[str, str]:
    """The label as plain fields, for embedding into csv/json artifacts."""
    return {
        "SURVIVORSHIP_BIASED": SURVIVORSHIP_BIASED,
        "corpus_id": config.CORPUS_ID,
        "purpose": config.PURPOSE,
    }


def attach_parquet_metadata(table: Any) -> Any:
    """Return `table` with the survivorship label merged into its kv metadata.

    Preserves any existing schema metadata (notably ``ARROW:schema`` and
    ``pandas``) so round-tripping through pandas keeps working.
    """
    existing = dict(table.schema.metadata or {})
    existing.update(PARQUET_METADATA)
    return table.replace_schema_metadata(existing)


def assert_parquet_is_labelled(path: Path) -> None:
    """Raise if the parquet at `path` lacks the survivorship label.

    Used by the finalize self-check so an unlabelled file cannot ship.
    """
    import pyarrow.parquet as pq

    meta = pq.ParquetFile(str(path)).metadata.metadata or {}
    value = meta.get(b"SURVIVORSHIP_BIASED")
    if value != SURVIVORSHIP_BIASED.encode():
        raise AssertionError(
            f"{path} is missing SURVIVORSHIP_BIASED parquet metadata "
            f"(got {value!r}). Refusing to ship an unlabelled artifact."
        )


def write_labelled_csv(
    path: Path, rows: Iterable[Mapping[str, Any]], fieldnames: Sequence[str]
) -> None:
    """Write a csv whose every row carries the survivorship label as columns.

    The label is per-row rather than a header comment on purpose: a consumer
    slicing the file with pandas keeps the label attached to the numbers.
    """
    label = label_fields()
    out_fields = list(fieldnames) + [k for k in label if k not in fieldnames]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=out_fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, **label})


def write_labelled_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write json with the survivorship label hoisted to the top-level keys."""
    body = {
        "SURVIVORSHIP_BIASED": SURVIVORSHIP_BIASED,
        "survivorship_note": SURVIVORSHIP_NOTE,
        "corpus_id": config.CORPUS_ID,
        "purpose": config.PURPOSE,
        **payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(body, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def artifact_carries_label(path: Path) -> bool:
    """Best-effort check that a shipped numeric artifact carries the label."""
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        try:
            assert_parquet_is_labelled(path)
            return True
        except (AssertionError, OSError):
            return False
    if suffix in {".csv", ".json", ".log", ".md", ".txt"}:
        try:
            return "SURVIVORSHIP_BIASED" in path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return False
    return True
