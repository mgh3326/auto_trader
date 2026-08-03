"""Shared adapter binding for inferred local corpus contracts.

Adapters use this thin object instead of reimplementing the KR harness guards:
``loader`` performs SHA-before-parse and row-date refusal, ``holdout_guard``
performs case-fold/symlink/path traversal refusal, and ``schema_contract``
performs exact-schema refusal. Contract files are committed declarations, not
real corpus artifacts.
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
from schema_contract import arrow_schema_for, load_contract, validate_table_schema

__all__ = [
    "ContractBackedCorpusAdapter",
    "TimestampContractError",
    "parse_utc_timestamp",
]


class TimestampContractError(ValueError):
    """A timestamp_utc field is absent, naive, malformed, or non-UTC."""


def parse_utc_timestamp(value: datetime | str) -> datetime:
    """Parse and retain an aware UTC timestamp without local re-anchoring.

    Contract rows carry ``timestamp_utc`` as an ISO-8601 string. A non-UTC
    offset and a naive value are loud failures: silently interpreting either as
    ET/KST would recreate the US daily-candle date-shift failure mode.
    """
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
    """Bind a local schema declaration and holdout policy to shared loaders."""

    contract_path: Path
    holdout_policy: HoldoutPolicy

    def contract(self) -> dict:
        return load_contract(self.contract_path)

    def arrow_schema_for(self, dataset: str) -> pa.Schema:
        return arrow_schema_for(dataset, contract_path=self.contract_path)

    def validate_table_schema(self, table: pa.Table, dataset: str) -> None:
        validate_table_schema(table, dataset, contract_path=self.contract_path)

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
        """Load only through the shared SHA-before-parse loader."""
        return load_shard(
            artifact_root,
            entry,
            allowed_window_start=allowed_window_start,
            allowed_window_end=allowed_window_end,
            contract_path=self.contract_path,
            holdout_policy=self.holdout_policy,
        )
