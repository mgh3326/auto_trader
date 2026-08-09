"""Canonical parquet schemas and manifest key sets for ``DFC_V22_RESEARCH_MIN``.

Decimal-bearing fields are typed ``string`` on purpose.  The admissibility test
(A2-C5) is an independent verifier re-collecting the same public object and
comparing raw payload hashes; a float round-trip destroys exactly the bytes that
comparison depends on, so the raw token is carried through unchanged and any
arithmetic happens at read time.

The schemas are exact: a table may carry neither fewer nor more columns than the
schema declares, and every column's type must match.  "Extra column" is the shape
that both a smuggled forbidden source (A2-C3) and a free bool/price input
(A2-C4) take, so the closed schema is load-bearing rather than tidy.
"""

from __future__ import annotations

from types import MappingProxyType

import pyarrow as pa

__all__ = [
    "CANONICAL_TABLES",
    "KLINES_4H_SCHEMA",
    "MANIFEST_REQUIRED_KEYS",
    "MANIFEST_SOURCE_REQUIRED_KEYS",
    "MANIFEST_TABLE_REQUIRED_KEYS",
    "OUTCOMES_SCHEMA",
    "PIT_UNIVERSE_SCHEMA",
    "PREMIUM_INDEX_4H_SCHEMA",
    "RAW_KLINE_FIELD_NAMES",
]

_TS = pa.timestamp("us", tz="UTC")


def _provenance_fields() -> list[pa.Field]:
    """Per-row link back to the object the row was parsed out of (A2-C5)."""
    return [
        pa.field("endpoint", pa.string(), nullable=False),
        pa.field("query", pa.string(), nullable=False),
        pa.field("retrieved_at", _TS, nullable=False),
        pa.field("source_schema", pa.string(), nullable=False),
        pa.field("object_sha256", pa.string(), nullable=False),
        pa.field("payload_sha256", pa.string(), nullable=False),
    ]


#: The twelve fields of a Binance USD-M kline, in wire order, under their
#: canonical names.  Required source material per A2-C3 — none may be dropped,
#: not even the unused trailing ``ignore`` element.
RAW_KLINE_FIELD_NAMES: tuple[str, ...] = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
    "ignore",
)

KLINES_4H_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("open_time", pa.int64(), nullable=False),
        pa.field("open", pa.string(), nullable=False),
        pa.field("high", pa.string(), nullable=False),
        pa.field("low", pa.string(), nullable=False),
        pa.field("close", pa.string(), nullable=False),
        pa.field("volume", pa.string(), nullable=False),
        pa.field("close_time", pa.int64(), nullable=False),
        pa.field("quote_asset_volume", pa.string(), nullable=False),
        pa.field("number_of_trades", pa.int64(), nullable=False),
        pa.field("taker_buy_base_asset_volume", pa.string(), nullable=False),
        pa.field("taker_buy_quote_asset_volume", pa.string(), nullable=False),
        pa.field("ignore", pa.string(), nullable=False),
        *_provenance_fields(),
    ]
)

PREMIUM_INDEX_4H_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("open_time", pa.int64(), nullable=False),
        pa.field("close_time", pa.int64(), nullable=False),
        pa.field("premium_index_close", pa.string(), nullable=False),
        *_provenance_fields(),
    ]
)

#: Point-in-time universe.  Eligibility is carried as a hash of the lifecycle
#: evidence that decided it, never as a bare boolean somebody could set by hand.
PIT_UNIVERSE_SCHEMA = pa.schema(
    [
        pa.field("epoch_open_time", pa.int64(), nullable=False),
        pa.field("rank", pa.int32(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("quote_volume_lookback", pa.string(), nullable=False),
        pa.field("lookback_start_time", pa.int64(), nullable=False),
        pa.field("lookback_end_time", pa.int64(), nullable=False),
        pa.field("lifecycle_evidence_sha256", pa.string(), nullable=False),
        pa.field("eligibility_evidence_sha256", pa.string(), nullable=False),
        *_provenance_fields(),
    ]
)

#: One row per ``BasketDecision``.  Every column is either an identifier, a
#: reference into ``klines_4h``, or the single derived outcome number.
OUTCOMES_SCHEMA = pa.schema(
    [
        pa.field("signal_epoch_open_time", pa.int64(), nullable=False),
        pa.field("signal_epoch_close_time", pa.int64(), nullable=False),
        pa.field("candidate_any", pa.string(), nullable=False),
        pa.field("winner_symbol", pa.string(), nullable=False),
        pa.field("t_kline_open_time", pa.int64(), nullable=False),
        pa.field("t_close_payload_sha256", pa.string(), nullable=False),
        pa.field("next_kline_open_time", pa.int64(), nullable=False),
        pa.field("next_kline_close_time", pa.int64(), nullable=False),
        pa.field("next_close_payload_sha256", pa.string(), nullable=False),
        pa.field("outcome_abs_log_return_bps", pa.float64(), nullable=False),
        pa.field("evidence_status", pa.string(), nullable=False),
    ]
)

CANONICAL_TABLES: MappingProxyType[str, pa.Schema] = MappingProxyType(
    {
        "klines_4h": KLINES_4H_SCHEMA,
        "premium_index_4h": PREMIUM_INDEX_4H_SCHEMA,
        "pit_universe": PIT_UNIVERSE_SCHEMA,
        "outcomes": OUTCOMES_SCHEMA,
    }
)


MANIFEST_REQUIRED_KEYS: tuple[str, ...] = (
    "contract_id",
    "contract_doc_sha256",
    "corpus_id",
    "root",
    "frozen",
    "warmup",
    "judgment_window",
    "outcome_tail_bars",
    "universe",
    "imputation",
    "prerequisite_readiness",
    "sources",
    "tables",
    "admissibility",
)

MANIFEST_SOURCE_REQUIRED_KEYS: tuple[str, ...] = (
    "name",
    "kind",
    "endpoint",
    "query",
    "retrieved_at",
    "schema",
    "object_kind",
    "object_sha256",
    "payload_sha256_by_epoch",
    "byte_size",
    "row_count",
)

MANIFEST_TABLE_REQUIRED_KEYS: tuple[str, ...] = (
    "name",
    "path",
    "sha256",
    "row_count",
    "byte_size",
)
