"""Shared adapter binding for sealed-corpus contracts.

Adapters use this thin object instead of reimplementing the KR harness guards:
``loader`` performs SHA-before-parse and row-date refusal, ``holdout_guard``
performs case-fold/symlink/path traversal refusal, and ``schema_contract``
performs schema + corpus-kind table_load_policy refusal.

**Contract selection is enum-only.** Callers pass ``CorpusKind``; the committed
contract path is resolved inside ``schema_contract.contract_path_for``. There is
no public ``contract_path`` constructor argument or load keyword.

Caveat (accurate residual boundary — what is gated vs what is not):

* **Gated on supported harness paths:** loads that take ``CorpusKind`` use the
  internal committed contract for that kind. There is no caller ``contract_path``
  input, so temp-JSON policy strip / corpus_id swap cannot be supplied as an
  argument.
* **Not gated (same class as raw PyArrow):** (1) reading parquet via raw
  ``pyarrow`` without this harness; (2) Python module-attribute monkeypatch
  (e.g. replacing ``schema_contract._CORPUS_TABLE_LOAD_POLICY_BY_ID``). MappingProxy
  only stops *naive in-place* mutation of the registry object; it does **not**
  stop ``module._NAME = evil``. We do not claim full process-wide sealing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
from holdout_guard import (
    HoldoutPolicy,
    assert_date_not_holdout,
    assert_path_not_holdout,
    assert_range_not_holdout,
)
from loader import ManifestEntry, load_manifest, load_shard
from schema_contract import (
    CorpusKind,
    arrow_schema_for,
    contract_path_for,
    load_contract,
    validate_table_schema,
)

__all__ = [
    "ContractBackedCorpusAdapter",
    "TimestampContractError",
    "parse_utc_timestamp",
]


class TimestampContractError(ValueError):
    """A timestamp_utc field is absent, naive, malformed, or non-UTC."""


def parse_utc_timestamp(value: datetime | str) -> datetime:
    """Parse and retain an aware UTC timestamp without local re-anchoring."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TimestampContractError(
                f"timestamp_utc must be ISO-8601 UTC, got {value!r}"
            ) from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TimestampContractError(
            f"timestamp_utc must be datetime|str, got {type(value)!r}"
        )

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TimestampContractError("timestamp_utc must be timezone-aware UTC")
    if parsed.utcoffset() != timedelta(0):
        raise TimestampContractError(
            "timestamp_utc must have a UTC (+00:00) offset; local anchors are refused"
        )
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class ContractBackedCorpusAdapter:
    """Bind a ``CorpusKind`` (not a file path) to shared loaders."""

    corpus: CorpusKind
    holdout_policy: HoldoutPolicy

    @property
    def contract_path(self) -> Path:
        """Committed path for this kind (read-only; not a constructor input)."""
        return contract_path_for(self.corpus)

    def contract(self) -> dict:
        return load_contract(self.corpus)

    def arrow_schema_for(self, dataset: str) -> pa.Schema:
        return arrow_schema_for(dataset, corpus=self.corpus)

    def validate_table_schema(self, table: pa.Table, dataset: str) -> None:
        """Schema + registry table_load_policy for this corpus kind."""
        validate_table_schema(table, dataset, corpus=self.corpus)

    def assert_path_allowed(self, path: Path | str) -> Path:
        return assert_path_not_holdout(path, policy=self.holdout_policy)

    def assert_date_allowed(self, value: date | datetime | str) -> date:
        return assert_date_not_holdout(value, policy=self.holdout_policy)

    def assert_range_allowed(
        self,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> tuple[date, date]:
        return assert_range_not_holdout(start, end, policy=self.holdout_policy)

    def load_manifest(self, manifest_path: Path | str) -> list[ManifestEntry]:
        """Load only through the shared dual-gated manifest loader."""
        return load_manifest(manifest_path, holdout_policy=self.holdout_policy)

    def load_shard(
        self,
        artifact_root: Path | str,
        entry: ManifestEntry,
        *,
        allowed_window_start: date | str | None = None,
        allowed_window_end: date | str | None = None,
    ) -> pa.Table:
        """Load via shared loader; schema/policy fixed by ``self.corpus``."""
        return load_shard(
            artifact_root,
            entry,
            allowed_window_start=allowed_window_start,
            allowed_window_end=allowed_window_end,
            corpus=self.corpus,
            holdout_policy=self.holdout_policy,
        )
