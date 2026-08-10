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
    "GAP_EXCLUSION_REQUIRED_KEYS",
    "KLINES_4H_SCHEMA",
    "MANIFEST_GAP_REQUIRED_KEYS",
    "MANIFEST_LIFECYCLE_REQUIRED_KEYS",
    "MANIFEST_RANKING_INPUT_DEFICIT_REQUIRED_KEYS",
    "MANIFEST_REQUIRED_KEYS",
    "MANIFEST_SOURCE_REQUIRED_KEYS",
    "MANIFEST_TABLE_REQUIRED_KEYS",
    "OUTCOMES_SCHEMA",
    "PIT_UNIVERSE_SCHEMA",
    "PREMIUM_INDEX_4H_SCHEMA",
    "PREMIUM_INDEX_GAP_AUDIT_SCHEMA",
    "RANKING_INPUT_DEFICIT_ROW_REQUIRED_KEYS",
    "RAW_KLINE_FIELD_NAMES",
    "SAMPLE_REPORT_REQUIRED_KEYS",
    "SAMPLE_ROW_REQUIRED_KEYS",
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
#:
#: ``candidate_any`` is the NW-F4 *arm label*.  The wire type is ``string``
#: because a label is a name, and it is a **closed** domain: the only two
#: admissible values are ``contract.ARM_LABELS``.  Arrow can pin the type but
#: not the domain, so ``validation._require_arm_label`` pins the domain — an
#: arbitrary string is as inadmissible here as a boolean is.
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

#: One row per (epoch, in-window gap symbol) — A2-C7.  A symbol whose premium
#: index the archive does not carry has to be *accounted for* at every epoch it
#: could have ranked in, so that dropping it from the pool is a visible act
#: rather than an absence.
#:
#: ``quote_volume_lookback`` is the one nullable column in the corpus, and the
#: validator pins it both ways: present exactly when the row claims the symbol
#: was eligible, absent exactly when it claims it was not.  A placeholder zero
#: would read as "ranked last" rather than "not ranked", which is the confusion
#: this column exists to prevent.
PREMIUM_INDEX_GAP_AUDIT_SCHEMA = pa.schema(
    [
        pa.field("epoch_open_time", pa.int64(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("eligibility_status", pa.string(), nullable=False),
        pa.field("eligibility_evidence_sha256", pa.string(), nullable=False),
        pa.field("quote_volume_lookback", pa.string(), nullable=True),
        pa.field("verdict", pa.string(), nullable=False),
        *_provenance_fields(),
    ]
)

CANONICAL_TABLES: MappingProxyType[str, pa.Schema] = MappingProxyType(
    {
        "klines_4h": KLINES_4H_SCHEMA,
        "premium_index_4h": PREMIUM_INDEX_4H_SCHEMA,
        "pit_universe": PIT_UNIVERSE_SCHEMA,
        "premium_index_gap_audit": PREMIUM_INDEX_GAP_AUDIT_SCHEMA,
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
    "lifecycle_eligibility",
    "premium_index_gap",
    "ranking_input_deficit",
    "sources",
    "tables",
    "admissibility",
)

#: A2-C6 / A2-C7.  ``symbols`` is the full archive diff; ``in_window_symbols``
#: is the subset that gets a per-epoch audit row; ``exclusions`` must account
#: for every symbol in the difference between the two.
MANIFEST_GAP_REQUIRED_KEYS: tuple[str, ...] = (
    "symbols",
    "in_window_symbols",
    "exclusions",
    "listing_endpoint",
    "listing_retrieved_at",
    "measurement_sha256",
)

GAP_EXCLUSION_REQUIRED_KEYS: tuple[str, ...] = (
    "symbol",
    "reason",
    "evidence_sha256",
)

#: A2-C9 (OD-31).  The manifest restates the frozen enumeration rather than
#: pointing at it, and both the source digest and the epoch list are compared
#: against ``contract`` — a corpus that quietly widened, narrowed or re-derived
#: the list has to say so here, where the comparison happens.
MANIFEST_RANKING_INPUT_DEFICIT_REQUIRED_KEYS: tuple[str, ...] = (
    "enumeration_path",
    "enumeration_sha256",
    "epochs",
    "verdict",
    "rows",
)

#: One entry per enumerated epoch: the epoch, the rank-3 symbol the deficient
#: archive input actually ranks (which the corpus records), and the rank-3
#: symbol complete input would have ranked (which it must not).
RANKING_INPUT_DEFICIT_ROW_REQUIRED_KEYS: tuple[str, ...] = (
    "epoch_open_time",
    "as_archived_rank3",
    "would_have_been_rank3",
)

#: A2-C6.  The corpus states the authority situation rather than leaving it
#: implicit, so a later reader cannot assume one was found.
MANIFEST_LIFECYCLE_REQUIRED_KEYS: tuple[str, ...] = (
    "authoritative_public_source",
    "evidence_kind",
    "proxy_limits",
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

#: A2-C8 (Job B).  One row per (sampled epoch, ranked candidate symbol). This
#: is a *readiness report*, not a corpus table — it is judged before any
#: corpus manifest exists, so it carries no ``CANONICAL_TABLES`` entry.
SAMPLE_ROW_REQUIRED_KEYS: tuple[str, ...] = (
    "quarter",
    "epoch_open_time",
    "rank",
    "symbol",
    "kline_complete",
    "premium_index_complete",
    "missing_detail",
    "provenance_sha256",
)

#: Top-level readiness report a Job-B measurement must produce. ``sample_rule``
#: is compared against ``contract.SAMPLE_RULE`` verbatim; ``quarters`` is
#: compared against ``contract.sample_plan()`` verbatim — both recomputed, not
#: merely schema-checked, so a report cannot claim a sample it did not draw.
SAMPLE_REPORT_REQUIRED_KEYS: tuple[str, ...] = (
    "contract_id",
    "contract_doc_sha256",
    "sample_rule",
    "quarters",
    "rows",
    "run_invalid_epochs",
    "verdict",
    "measured_at",
)
