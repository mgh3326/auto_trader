"""Venue-aware, fail-closed reader for labeled crypto corpus Parquet files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pyarrow as pa
import pyarrow.parquet as pq

from .policy import (
    CrossVenueReadForbidden,
    HoldoutReadForbidden,
    ParquetPolicyMismatchError,
    UpbitXsecOptInRequired,
    VenuePolicy,
    policy_from_parquet_metadata,
)

ConsumerIntent = Literal["time_series", "xsec"]


@dataclass(frozen=True)
class LabeledCorpus:
    """Data returned only after all file labels and intent guards passed."""

    table: pa.Table
    policy: VenuePolicy
    source_paths: tuple[Path, ...]
    consumer_intent: ConsumerIntent


def _reject_holdout_path(path: Path) -> None:
    if "holdout" in path.parts:
        raise HoldoutReadForbidden(
            "holdout is write-only for crypto-corpus-v1 and cannot be loaded"
        )


def inspect_parquet_policy(path: str | Path) -> VenuePolicy:
    """Read and validate only one non-holdout file's policy metadata."""
    normalized = Path(path)
    _reject_holdout_path(normalized)
    parquet_file = pq.ParquetFile(normalized)
    return policy_from_parquet_metadata(parquet_file.schema_arrow.metadata)


def _validate_table_venue(
    table: pa.Table,
    *,
    policy: VenuePolicy,
    path: Path,
) -> None:
    if "venue" not in table.column_names:
        raise ParquetPolicyMismatchError(
            f"{path}: labeled corpus table does not contain a venue column"
        )
    observed_venues = set(table.column("venue").to_pylist())
    if observed_venues != {policy.venue}:
        raise ParquetPolicyMismatchError(
            f"{path}: row venue values {sorted(observed_venues)!r} do not match "
            f"metadata venue {policy.venue!r}"
        )


def load_labeled_parquet_files(
    paths: list[str | Path] | tuple[str | Path, ...],
    *,
    consumer_intent: ConsumerIntent = "time_series",
    allow_upbit_survivorship_biased_xsec: bool = False,
) -> LabeledCorpus:
    """Load one venue only, after label validation and XSEC intent enforcement.

    File footers are checked for every required label before the first row group
    is read. An unlabeled file, an incompatible venue, or a disallowed Upbit
    cross-sectional request therefore fails with an exception instead of a
    filtered or empty result.
    """
    if consumer_intent not in {"time_series", "xsec"}:
        raise ValueError(f"unsupported consumer intent: {consumer_intent!r}")
    normalized_paths = tuple(Path(path) for path in paths)
    if not normalized_paths:
        raise ValueError("at least one labeled Parquet file is required")

    policies = tuple(inspect_parquet_policy(path) for path in normalized_paths)
    venues = {policy.venue for policy in policies}
    if len(venues) != 1:
        raise CrossVenueReadForbidden(
            "crypto-corpus-v1 files from separate venues cannot be read together"
        )
    policy = policies[0]
    if (
        consumer_intent == "xsec"
        and policy.venue == "upbit_krw"
        and not allow_upbit_survivorship_biased_xsec
    ):
        raise UpbitXsecOptInRequired(
            "Upbit XSEC requires "
            "allow_upbit_survivorship_biased_xsec=True because its active "
            "universe is survivor-biased"
        )

    tables: list[pa.Table] = []
    for path, file_policy in zip(normalized_paths, policies, strict=True):
        table = pq.ParquetFile(path).read()
        _validate_table_venue(table, policy=file_policy, path=path)
        tables.append(table)
    combined = tables[0] if len(tables) == 1 else pa.concat_tables(tables)
    return LabeledCorpus(
        table=combined,
        policy=policy,
        source_paths=normalized_paths,
        consumer_intent=consumer_intent,
    )


def load_labeled_parquet(
    path: str | Path,
    *,
    consumer_intent: ConsumerIntent = "time_series",
    allow_upbit_survivorship_biased_xsec: bool = False,
) -> LabeledCorpus:
    """Load one labeled file through the same fail-closed policy gate."""
    return load_labeled_parquet_files(
        (path,),
        consumer_intent=consumer_intent,
        allow_upbit_survivorship_biased_xsec=allow_upbit_survivorship_biased_xsec,
    )
