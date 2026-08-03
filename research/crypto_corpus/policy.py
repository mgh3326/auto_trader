"""Fail-closed venue policy labels for the crypto-corpus-v1 Parquet files.

The labels live in each file's Arrow schema metadata rather than solely in a
manifest. Consumers must use the guarded loader so an unlabeled file, a
mislabelled file, or an unacknowledged Upbit cross-sectional request raises an
exception before data is returned.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pyarrow as pa

from .constants import CORPUS_ID

POLICY_SCHEMA_VERSION = "1"
CONSUMER_CONTRACT = "PARQUET_METADATA_REQUIRED_FAIL_CLOSED"

METADATA_POLICY_SCHEMA_VERSION = b"crypto_corpus.policy_schema_version"
METADATA_CORPUS_ID = b"crypto_corpus.corpus_id"
METADATA_VENUE = b"crypto_corpus.venue"
METADATA_SURVIVORSHIP_LABEL = b"crypto_corpus.survivorship_label"
METADATA_XSEC_POLICY = b"crypto_corpus.xsec_policy"
METADATA_CONSUMER_CONTRACT = b"crypto_corpus.consumer_contract"


class CorpusPolicyError(ValueError):
    """A file cannot be safely consumed under the corpus venue contract."""


class UnlabeledParquetError(CorpusPolicyError):
    """Required per-file policy metadata is absent."""


class ParquetPolicyMismatchError(CorpusPolicyError):
    """A present policy label is malformed or contradicts its venue."""


class UpbitXsecOptInRequired(CorpusPolicyError):
    """Upbit's survivor-biased universe needs explicit XSEC acknowledgement."""


class CrossVenueReadForbidden(CorpusPolicyError):
    """A read attempted to combine venue-separated corpus files."""


class HoldoutReadForbidden(PermissionError):
    """The corpus job's holdout tree is never a readable loader source."""


@dataclass(frozen=True)
class VenuePolicy:
    """The bias and XSEC contract attached to one venue's Parquet files."""

    venue: str
    survivorship_label: str
    xsec_policy: str


VENUE_POLICIES: dict[str, VenuePolicy] = {
    "upbit_krw": VenuePolicy(
        venue="upbit_krw",
        survivorship_label="SURVIVORSHIP_BIASED",
        xsec_policy="XSEC_EXPLICIT_UPBIT_OPT_IN_REQUIRED",
    ),
    "binance_usdt_spot": VenuePolicy(
        venue="binance_usdt_spot",
        survivorship_label="DELISTED_AVAILABLE_DEGRADED",
        xsec_policy="XSEC_DEGRADED_SINGLE_VENUE_ONLY",
    ),
}


def venue_policy(venue: str) -> VenuePolicy:
    """Return the literal policy for a supported, separate venue."""
    try:
        return VENUE_POLICIES[venue]
    except KeyError as exc:
        raise ParquetPolicyMismatchError(
            f"unsupported corpus venue: {venue!r}"
        ) from exc


def policy_metadata(venue: str) -> dict[bytes, bytes]:
    """Build the canonical metadata fields for one venue."""
    policy = venue_policy(venue)
    return {
        METADATA_POLICY_SCHEMA_VERSION: POLICY_SCHEMA_VERSION.encode(),
        METADATA_CORPUS_ID: CORPUS_ID.encode(),
        METADATA_VENUE: policy.venue.encode(),
        METADATA_SURVIVORSHIP_LABEL: policy.survivorship_label.encode(),
        METADATA_XSEC_POLICY: policy.xsec_policy.encode(),
        METADATA_CONSUMER_CONTRACT: CONSUMER_CONTRACT.encode(),
    }


def _metadata_value(metadata: Mapping[bytes, bytes], key: bytes) -> str:
    raw_value = metadata.get(key)
    if raw_value is None:
        raise UnlabeledParquetError(
            f"required Parquet policy metadata is missing: {key.decode()}"
        )
    try:
        value = raw_value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParquetPolicyMismatchError(
            f"Parquet policy metadata is not UTF-8: {key.decode()}"
        ) from exc
    if not value:
        raise UnlabeledParquetError(
            f"required Parquet policy metadata is empty: {key.decode()}"
        )
    return value


def policy_from_parquet_metadata(
    metadata: Mapping[bytes, bytes] | None,
    *,
    expected_venue: str | None = None,
) -> VenuePolicy:
    """Validate and resolve a venue policy from file-level Parquet metadata."""
    if metadata is None:
        raise UnlabeledParquetError("Parquet file has no schema metadata")

    schema_version = _metadata_value(metadata, METADATA_POLICY_SCHEMA_VERSION)
    corpus_id = _metadata_value(metadata, METADATA_CORPUS_ID)
    venue = _metadata_value(metadata, METADATA_VENUE)
    survivorship_label = _metadata_value(metadata, METADATA_SURVIVORSHIP_LABEL)
    xsec_policy = _metadata_value(metadata, METADATA_XSEC_POLICY)
    consumer_contract = _metadata_value(metadata, METADATA_CONSUMER_CONTRACT)

    if schema_version != POLICY_SCHEMA_VERSION:
        raise ParquetPolicyMismatchError(
            f"unsupported policy schema version: {schema_version!r}"
        )
    if corpus_id != CORPUS_ID:
        raise ParquetPolicyMismatchError(
            f"metadata corpus_id {corpus_id!r} is not {CORPUS_ID!r}"
        )
    if consumer_contract != CONSUMER_CONTRACT:
        raise ParquetPolicyMismatchError(
            "Parquet consumer contract is not fail-closed metadata enforcement"
        )

    policy = venue_policy(venue)
    if survivorship_label != policy.survivorship_label:
        raise ParquetPolicyMismatchError(
            f"{venue}: survivorship label {survivorship_label!r} is not "
            f"{policy.survivorship_label!r}"
        )
    if xsec_policy != policy.xsec_policy:
        raise ParquetPolicyMismatchError(
            f"{venue}: XSEC policy {xsec_policy!r} is not {policy.xsec_policy!r}"
        )
    if expected_venue is not None and policy.venue != expected_venue:
        raise ParquetPolicyMismatchError(
            f"Parquet metadata venue {policy.venue!r} does not match "
            f"expected {expected_venue!r}"
        )
    return policy


def label_table_for_venue(table: pa.Table, venue: str) -> pa.Table:
    """Attach a canonical policy label without changing table values or fields."""
    existing_metadata = dict(table.schema.metadata or {})
    if METADATA_VENUE in existing_metadata:
        policy_from_parquet_metadata(existing_metadata, expected_venue=venue)
    existing_metadata.update(policy_metadata(venue))
    return table.replace_schema_metadata(existing_metadata)
