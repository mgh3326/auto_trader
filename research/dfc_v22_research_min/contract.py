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
``A2-C6``     OD-26             Lifecycle authority absence and substitute
``A2-C7``     OD-26             Premium-index archive gap and epoch verdict
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
    "ARM_CANDIDATE",
    "ARM_CONTROL",
    "ARM_LABELS",
    "BAR_INTERVAL_MS",
    "CANONICAL_SOURCE_PATH",
    "CANONICAL_SOURCE_SHA256",
    "CLAUSE_SOURCES",
    "CONTRACT_DOC_RELPATH",
    "CONTRACT_ID",
    "CORPUS_ID",
    "CORPUS_ROOT",
    "ELIGIBILITY_EVIDENCE_KIND",
    "FORBIDDEN_SOURCE_KINDS",
    "GAP_EPOCH_VERDICTS",
    "GAP_EXCLUSION_REASONS",
    "IMPUTED_ROW_COUNT_MAX",
    "JUDGMENT_END",
    "JUDGMENT_START",
    "LIFECYCLE_AUTHORITATIVE_PUBLIC_SOURCE",
    "LIFECYCLE_PROXY_LIMITS",
    "NO_IMPACT",
    "OUTCOME_HORIZON_BARS",
    "OUTCOME_TAIL_BARS",
    "OUTCOME_UNIT",
    "REQUIRED_SOURCE_KINDS",
    "RUN_INVALID_INPUT_EVIDENCE",
    "TERMINAL_CODE_PRIORITY",
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

# DFC v2.2 arm-label wire contract (shared; byte-identical in both registrations)
# =============================================================================
# NW-F4 says ``BasketDecision.candidate_any`` *is an arm label*.  It is therefore
# carried as a label, never as a truth value, and the label domain is closed:
#
#     ARM_CANDIDATE = "candidate"
#     ARM_CONTROL   = "control"
#     ARM_LABELS    = ("candidate", "control")
#
# Counterpart declaration: ``research_contracts/dfc_2c_4h_v22.py`` (PR #1825),
# which declares the same three literals in the same order.  Both sides reject
# anything outside this set and neither side coerces: a value that is not
# exactly one of these two labels never becomes an arm, it becomes
# ``RUN_INVALID_OUTCOME_EVIDENCE``.  A bare ``bool`` is rejected explicitly and
# ahead of the membership test, because ``bool`` is precisely the shape NW-F4
# forbids ("자유 bool ... 입력을 금지한다") and precisely the shape a silent
# ``bool(...)`` coercion would manufacture.
ARM_CANDIDATE = "candidate"
ARM_CONTROL = "control"
ARM_LABELS: tuple[str, ...] = (ARM_CANDIDATE, ARM_CONTROL)

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


# --- A2-C6 (OD-26): lifecycle authority absence and substitute evidence ---

#: There is no single authoritative public Binance source for contract
#: lifecycle / eligibility.  This is a measured absence, not a gap in the
#: search: A2-MEASURE looked and found two partial, non-identical proxies and
#: one corroborated historical case.  The value is ``None`` because writing a
#: source name here would be inventing one.
LIFECYCLE_AUTHORITATIVE_PUBLIC_SOURCE: str | None = None

#: The two proxies that do exist, each with the question it cannot answer.
#: They are recorded so that a later reader cannot mistake either for the
#: authority that A2-MEASURE established does not exist.
LIFECYCLE_PROXY_LIMITS: MappingProxyType[str, str] = MappingProxyType(
    {
        "exchange_info_onboard_date": (
            "covers only contracts that are currently trading; delisted and "
            "settled contracts are absent from the endpoint entirely, so the "
            "historical universe cannot be reconstructed from it. Its "
            "onboardDate is also not the first tradable epoch — UNFIUSDT's "
            "first 4h kline is 10 days after its onboardDate."
        ),
        "archive_month_range": (
            "first/last monthly object per symbol is an inference from data "
            "presence, not a lifecycle record Binance publishes and stands "
            "behind; it was corroborated against exactly one real event "
            "(LUNAUSDT, 2022-05) and says nothing about intra-month halts."
        ),
    }
)

#: The substitute defined by OD-26.  This is a *different kind of evidence*
#: from a lifecycle record, not an approximation of one: a completed 4h kline
#: carrying non-zero traded volume is direct evidence that the contract was
#: tradable in that bar, whereas a lifecycle record would be a statement about
#: listing status.  Neither substitutes for the other in general; OD-26 rules
#: that for ranking-window eligibility, the direct evidence is what this
#: corpus uses.
ELIGIBILITY_EVIDENCE_KIND = "kline_archive_direct"
ELIGIBILITY_REQUIRES_COMPLETE_KLINES = True
ELIGIBILITY_REQUIRES_NONZERO_VOLUME = True

#: A proxy may be recorded as context, never declared as the eligibility
#: evidence kind, and never declared authoritative.
LIFECYCLE_PROXY_KINDS: frozenset[str] = frozenset(LIFECYCLE_PROXY_LIMITS)

ELIGIBLE = "eligible"
NOT_ELIGIBLE = "not_eligible"
ELIGIBILITY_STATUSES: tuple[str, ...] = (ELIGIBLE, NOT_ELIGIBLE)


# --- A2-C7 (OD-26): premium-index archive gap and the per-epoch verdict ---

#: The two literal outcomes of the gap/pool intersection, per epoch.
NO_IMPACT = "NO_IMPACT"
RUN_INVALID_INPUT_EVIDENCE = "RUN_INVALID_INPUT_EVIDENCE"
GAP_EPOCH_VERDICTS: tuple[str, ...] = (NO_IMPACT, RUN_INVALID_INPUT_EVIDENCE)

#: Why a symbol in the archive diff needs no per-epoch audit row.  Closed set:
#: every gap symbol must be accounted for by one of these, with evidence.
#: "It did not look relevant" is not on the list.
GAP_EXCLUSION_REASONS: tuple[str, ...] = (
    # dated delivery contract — outside UNIVERSE_INSTRUMENT_CLASS, and no
    # premium index exists for a non-perpetual in the first place
    "not_perpetual",
    # no kline evidence anywhere in the corpus span, so never eligible in it
    "no_kline_evidence_in_corpus_span",
    # an archive rename artifact of an instrument that also appears under its
    # base symbol, which does carry premium-index material
    "same_instrument_as_base",
)
#: ``same_instrument_as_base`` is the only reason that leans on a second
#: symbol, so it must name it and show the row-identity evidence.
GAP_EXCLUSION_BASE_SYMBOL_REASON = "same_instrument_as_base"

#: Terminal codes in adjudication order.  Input evidence is decided first: if
#: the corpus was built on material the contract refuses, every later verdict
#: is a statement about the wrong artifact.
TERMINAL_CODE_PRIORITY: tuple[str, ...] = (
    "RUN_INVALID_INPUT_EVIDENCE",
    "RUN_INVALID_SCOPE_SEPARATION",
    "RUN_INVALID_CORPUS_LITERALS",
    "RUN_INVALID_FORBIDDEN_SOURCE",
    "RUN_INVALID_MANIFEST_SCHEMA",
    "RUN_INVALID_TABLE_SCHEMA",
    "RUN_INVALID_AUTHENTICITY_EVIDENCE",
    "RUN_INVALID_OUTCOME_EVIDENCE",
)


CLAUSE_SOURCES: MappingProxyType[str, str] = MappingProxyType(
    {
        "A2-C1": "NW-F2",
        "A2-C2": "NW-F5",
        "A2-C3": "NW-F5",
        "A2-C4": "NW-F4",
        "A2-C5": "NW-F6",
        "A2-C6": "OD-26",
        "A2-C7": "OD-26",
    }
)
