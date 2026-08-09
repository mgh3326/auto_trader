"""Frozen literals for the ``DFC_V22_RESEARCH_MIN`` (A2) contract.

Every literal here is a mechanical transcription of one of the four verbatim
upstream clauses in :mod:`research.dfc_v22_research_min.nw_verbatim`.  Nothing
here may be relaxed, widened or re-derived after corpus results are read; the
whole point of registering this file before data contact is that the gate is
signed blind.

Clause IDs used across the contract document, the schemas and the golden cases:

============  ================  =========================================
Clause        Upstream source   Subject
============  ================  =========================================
``A2-C1``     NW-F2             Scope separation from FUT-DATA-A1
``A2-C2``     NW-F5             Corpus identity, windows and universe rule
``A2-C3``     NW-F5             Required / forbidden source material
``A2-C4``     NW-F4             Outcome semantics
``A2-C5``     NW-F6             Authenticity and freeze procedure
============  ================  =========================================
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

from .nw_verbatim import (
    CANONICAL_SOURCE_PATH,
    CANONICAL_SOURCE_SHA256,
    VERBATIM_CLAUSES,
)

__all__ = [
    "A1_READINESS_IDS",
    "A2_JUDGED_DIMENSIONS",
    "BAR_INTERVAL_MS",
    "CANONICAL_SOURCE_PATH",
    "CANONICAL_SOURCE_SHA256",
    "CLAUSE_SOURCES",
    "CONTRACT_DOC_RELPATH",
    "CONTRACT_ID",
    "CORPUS_ID",
    "CORPUS_ROOT",
    "FORBIDDEN_SOURCE_KINDS",
    "IMPUTED_ROW_COUNT_MAX",
    "JUDGMENT_END",
    "JUDGMENT_START",
    "OUTCOME_HORIZON_BARS",
    "OUTCOME_TAIL_BARS",
    "OUTCOME_UNIT",
    "REQUIRED_SOURCE_KINDS",
    "UNIVERSE_LOOKBACK_CALENDAR_DAYS",
    "UNIVERSE_RANKING_METRIC",
    "UNIVERSE_TIE_BREAK",
    "UNIVERSE_TOP_N",
    "VERBATIM_CLAUSES",
    "WARMUP_END",
    "WARMUP_START",
]


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


# --- A2-C1 (NW-F2): scope separation -------------------------------------

CONTRACT_ID = "DFC_V22_RESEARCH_MIN"
CONTRACT_DOC_RELPATH = "research/dfc_v22_research_min/contracts/DFC_V22_RESEARCH_MIN.md"

#: The only dimensions A2 judges.  A manifest that claims readiness on
#: anything else is not this contract.
A2_JUDGED_DIMENSIONS: tuple[str, ...] = (
    "kline_ofi",
    "premium_index",
    "pit_universe",
    "outcome_evidence",
)

#: FUT-DATA-A1 stays on the record as its own (``UNDETERMINED``) readiness
#: statement.  It is preserved, and it is *not* a prerequisite of DFC-v2.x.
A1_READINESS_IDS: frozenset[str] = frozenset(
    {"FUT-DATA-A1", "FUT_DATA_A1", "fut-data-a1", "fut_data_a1"}
)


# --- A2-C2 (NW-F5): corpus identity, windows, universe --------------------

CORPUS_ID = "dfc-2c-4h-v22-corpus-v1"
CORPUS_ROOT = "/Users/mgh3326/work/herdr-artifacts/dfc-2c-4h-v22-corpus-v1/"

#: Half-open ``[start, end)`` in UTC, exactly as signed.
WARMUP_START = _utc(2021, 2, 2)
WARMUP_END = _utc(2021, 5, 2)
JUDGMENT_START = _utc(2021, 5, 2)
JUDGMENT_END = _utc(2023, 8, 4)

#: One extra completed 4h bar past ``JUDGMENT_END`` so the last signal epoch
#: has an outcome.  Not a widening of the judgment window.
OUTCOME_TAIL_BARS = 1

BAR_INTERVAL_MS = 4 * 60 * 60 * 1000

UNIVERSE_LOOKBACK_CALENDAR_DAYS = 30
UNIVERSE_RANKING_METRIC = "quote_volume"
UNIVERSE_TOP_N = 3
UNIVERSE_TIE_BREAK = "canonical_symbol_ascending"
UNIVERSE_INSTRUMENT_CLASS = "usdm_perpetual"


# --- A2-C3 (NW-F5): required and forbidden source material ----------------

REQUIRED_SOURCE_KINDS: tuple[str, ...] = (
    "usdm_kline_4h",
    "premium_index_4h",
    "contract_lifecycle_eligibility",
)

#: Explicitly excluded from this corpus.  DFC-v2.2 signals do not read them,
#: so their presence means the corpus is not the one that was signed.
FORBIDDEN_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        "funding_rate",
        "funding",
        "open_interest",
        "open_interest_hist",
        "mark_price",
        "index_price",
    }
)

#: Column-name tokens that betray a forbidden source smuggled into a table.
FORBIDDEN_COLUMN_TOKENS: tuple[str, ...] = (
    "funding",
    "open_interest",
    "openinterest",
    "mark_price",
    "markprice",
    "index_price",
    "indexprice",
)

IMPUTED_ROW_COUNT_MAX = 0


# --- A2-C4 (NW-F4): outcome semantics -------------------------------------

#: ``candidate_any`` of the ``BasketDecision`` at signal epoch ``t``.
OUTCOME_ARM_LABEL_FIELD = "candidate_any"
#: The winner of that same decision.
OUTCOME_SYMBOL_FIELD = "winner_symbol"

OUTCOME_UNIT = "absolute_log_return_bps"
#: ``t`` close to the immediately next completed 4h close — one bar, never two.
OUTCOME_HORIZON_BARS = 1
#: Recomputation tolerance in bps; wider tolerances hide fabricated outcomes.
OUTCOME_BPS_TOLERANCE = 1e-6

#: The single terminal code for outcome-evidence failure.  Silent row removal
#: is the failure this code exists to make impossible.
RUN_INVALID_OUTCOME_EVIDENCE = "RUN_INVALID_OUTCOME_EVIDENCE"

#: An outcome row must be fully reconstructible from referenced raw bars.  No
#: operator-supplied booleans, no operator-supplied prices.
OUTCOME_EVIDENCE_STATUS = "raw_evidence_complete"
OUTCOME_FREE_PRICE_TOKENS: tuple[str, ...] = (
    "price",
    "entry",
    "exit",
    "fill",
    "pnl",
    "return_pct",
    "notional",
)


# --- A2-C5 (NW-F6): authenticity and freeze procedure ---------------------

#: Recorded on every original object/response.
REQUIRED_PROVENANCE_FIELDS: tuple[str, ...] = (
    "endpoint",
    "query",
    "retrieved_at",
    "schema",
    "object_sha256",
    "payload_sha256_by_epoch",
)

#: Admissibility is decided by an independent verifier re-collecting the same
#: public object and comparing these keys.  Self-consistency is not a basis.
ADMISSIBILITY_BASIS = "independent_recollection"
ADMISSIBILITY_FORBIDDEN_BASES: frozenset[str] = frozenset(
    {"self_consistency", "self-consistency", "internal_check"}
)
ADMISSIBILITY_COMPARISON_KEYS: tuple[str, ...] = (
    "object_sha256",
    "row_count",
    "time_bounds",
    "raw_payload_sha256",
)


CLAUSE_SOURCES: MappingProxyType[str, str] = MappingProxyType(
    {
        "A2-C1": "NW-F2",
        "A2-C2": "NW-F5",
        "A2-C3": "NW-F5",
        "A2-C4": "NW-F4",
        "A2-C5": "NW-F6",
    }
)
