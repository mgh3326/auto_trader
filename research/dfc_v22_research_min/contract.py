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
``A2-C8``     OD-26 (Job B)     Pre-registered sample readiness protocol
``A2-C9``     OD-31             Ranking-input deficit epochs (enumerated)
============  ================  =========================================
"""

from __future__ import annotations

import hashlib
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
    "NOT_READY",
    "OUTCOME_HORIZON_BARS",
    "OUTCOME_TAIL_BARS",
    "OUTCOME_UNIT",
    "RANKING_INPUT_DEFICIT_ENUMERATION_PATH",
    "RANKING_INPUT_DEFICIT_ENUMERATION_SHA256",
    "RANKING_INPUT_DEFICIT_EPOCHS",
    "RANKING_INPUT_DEFICIT_ROWS",
    "RANKING_INPUT_DEFICIT_SCAN_PATH",
    "RANKING_INPUT_DEFICIT_SCAN_RECORD_COUNT",
    "RANKING_INPUT_DEFICIT_SCAN_SHA256",
    "RANKING_INPUT_DEFICIT_UNCHANGED_HEAD",
    "RANKING_INPUT_DEFICIT_VERDICT",
    "READY",
    "REQUIRED_SOURCE_KINDS",
    "RUN_INVALID_INPUT_EVIDENCE",
    "SAMPLE_COMPLETENESS_DIMENSIONS",
    "SAMPLE_EPOCHS_PER_QUARTER",
    "SAMPLE_QUARTER_DEFINITION",
    "SAMPLE_RULE",
    "SAMPLE_SEED",
    "SAMPLE_VERDICTS",
    "TERMINAL_CODE_PRIORITY",
    "UNIVERSE_LOOKBACK_CALENDAR_DAYS",
    "UNIVERSE_RANKING_METRIC",
    "UNIVERSE_TIE_BREAK",
    "UNIVERSE_TOP_N",
    "VERBATIM_CLAUSES",
    "WARMUP_END",
    "WARMUP_START",
    "quarter_key",
    "quarter_windows",
    "sample_epoch_open_times",
    "sample_plan",
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


# --- A2-C9 (OD-31): ranking-input deficit epochs, enumerated and frozen ---

# §26차's A2-C7 trigger fires in one direction only: a gap symbol that is
# **inside** an epoch's top-3 pool.  §31차 measured the opposite direction — a
# ranking *input* deficit that kept a symbol **out** of the pool it belonged in
# — and ruled that reading A2-C7 as covering it would be widening a clause by
# interpretation.  So this is a separate clause with its own literal, and the
# set of epochs it applies to is an enumeration, not a rule: the list was fixed
# by a measurement that has already been read, and re-deriving it here would
# destroy the pre-registration it exists to preserve.

#: §34차 2항 amended the enumeration exactly once, 38 -> 49, and changed nothing
#: else about the clause.  The first 38 rows are the §31차 list reproduced
#: unchanged; the 11 added were measured over the *same* scan by a later job
#: that folded all 105 gap records into 8 windows and measured every one, where
#: the earlier measurement had covered a single window.  The amendment widens
#: what is enumerated, not what the clause permits: an added epoch is one more
#: epoch marked ``RUN_INVALID_INPUT_EVIDENCE``, never one fewer.

#: The measurement the enumeration is transcribed from.  Recorded as a path and
#: a digest, never opened: this package does not read files (see
#: ``test_package_does_not_write_files`` / ``..._cannot_reach_the_network``), so
#: the pin is what makes a changed enumeration visible rather than silent.
RANKING_INPUT_DEFICIT_ENUMERATION_PATH = (
    "~/work/herdr-artifacts/dfc-v22-readiness-v1/gap-impact-full/"
    "unified_flip_epochs.json"
)
RANKING_INPUT_DEFICIT_ENUMERATION_SHA256 = (
    "1dcf41ff108d2a9b98e821c93cabb242e31317cb202ed0650f38c962bda33cc4"
)

#: The gap scan the enumeration was measured *over*, pinned by its own digest.
#:
#: §33차 requires both digests because they answer different questions.  The
#: enumeration digest fixes *which epochs* are enumerated; this one fixes the
#: **scope the word "exhaustive" was earned against** — the 105 internal-gap
#: records, folded into 8 windows, that DFC-GAP-IMPACT-FULL measured one by one.
#: An enumeration is only "every affected epoch" relative to some scan, and a
#: scan that moved silently would leave the enumeration looking complete while
#: the ground under it changed.  Pinning one without the other is how a
#: completeness claim survives its own evidence being replaced.
RANKING_INPUT_DEFICIT_SCAN_PATH = (
    "~/work/herdr-artifacts/dfc-2c-4h-v22-corpus-v1/raw/phase3_investigation/"
    "internal_gaps.json"
)
RANKING_INPUT_DEFICIT_SCAN_SHA256 = (
    "f8cae492dddc58323211519d7b867a1de5efd5a06a56d4ee4aea40c0fca5050a"
)
#: Record count of that scan, carried as a second, independently checkable
#: handle on the same file: a digest says "changed", this says "how".
RANKING_INPUT_DEFICIT_SCAN_RECORD_COUNT = 105

#: Ranks 1 and 2 are the same two symbols at every one of the 49 epochs — a
#: measured property of the enumeration, not an assumption: only the rank-3
#: slot moves, which is why the rows below carry that slot alone.
RANKING_INPUT_DEFICIT_UNCHANGED_HEAD: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")

#: ``(epoch_open_time_ms, as_archived_rank3, would_have_been_rank3)``.
#:
#: ``as_archived_rank3`` is what the archive's own (deficient) ranking input
#: produces and therefore what the corpus records; ``would_have_been_rank3`` is
#: what the same rule produces once the deficit is made good.  The second column
#: is carried so the substitution can be *detected*, not so it can be performed:
#: writing it into the pool is the silent re-ranking §31차 forbids, and
#: ``validate_ranking_input_deficit`` rejects exactly that.
RANKING_INPUT_DEFICIT_ROWS: tuple[tuple[int, str, str], ...] = (
    (1646193600000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-02T04:00:00Z
    (1646208000000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-02T08:00:00Z
    (1646222400000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-02T12:00:00Z
    (1646236800000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-02T16:00:00Z
    (1646251200000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-02T20:00:00Z
    (1646265600000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-03T00:00:00Z
    (1646280000000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-03T04:00:00Z
    (1646294400000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-03T08:00:00Z
    (1646308800000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-03T12:00:00Z
    (1646323200000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-03T16:00:00Z
    (1646337600000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-03T20:00:00Z
    (1646352000000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-04T00:00:00Z
    (1646366400000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-04T04:00:00Z
    (1646380800000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-04T08:00:00Z
    (1646395200000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-04T12:00:00Z
    (1646409600000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-04T16:00:00Z
    (1646424000000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-04T20:00:00Z
    (1646438400000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-05T00:00:00Z
    (1646452800000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-05T04:00:00Z
    (1646467200000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-05T08:00:00Z
    (1646481600000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-05T12:00:00Z
    (1646496000000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-05T16:00:00Z
    (1646510400000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-05T20:00:00Z
    (1646524800000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-06T00:00:00Z
    (1646539200000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-06T04:00:00Z
    (1646553600000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-06T08:00:00Z
    (1646568000000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-06T12:00:00Z
    (1646582400000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-06T16:00:00Z
    (1646596800000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-06T20:00:00Z
    (1646611200000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-07T00:00:00Z
    (1646625600000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-07T04:00:00Z
    (1646640000000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-07T08:00:00Z
    (1646654400000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-07T12:00:00Z
    (1646668800000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-07T16:00:00Z
    (1646683200000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-07T20:00:00Z
    (1646697600000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-08T00:00:00Z
    (1646712000000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-08T04:00:00Z
    (1646726400000, "GALAUSDT", "LUNAUSDT"),  # 2022-03-08T08:00:00Z
    # --- second window (2022-03-31T20:00Z..2022-04-03T00:00Z, 50 symbols) ---
    # Added by §34차 2항.  Same mechanism, different window: GMTUSDT's own 12
    # missing bars shortened its trailing sum and kept it out of a top 3 it
    # belonged in.  These 11 were measured by DFC-GAP-IMPACT-FULL after the
    # first 38 were already frozen; the 38 above are reproduced unchanged.
    (1649116800000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-05T00:00:00Z
    (1649131200000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-05T04:00:00Z
    (1649145600000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-05T08:00:00Z
    (1649160000000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-05T12:00:00Z
    (1649174400000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-05T16:00:00Z
    (1649188800000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-05T20:00:00Z
    (1649203200000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-06T00:00:00Z
    (1649217600000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-06T04:00:00Z
    (1649232000000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-06T08:00:00Z
    (1649246400000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-06T12:00:00Z
    (1649260800000, "LUNAUSDT", "GMTUSDT"),  # 2022-04-06T16:00:00Z
)

#: The enumeration itself — the 49 epochs, in ascending order.
RANKING_INPUT_DEFICIT_EPOCHS: tuple[int, ...] = tuple(
    row[0] for row in RANKING_INPUT_DEFICIT_ROWS
)

#: The verdict §31차 assigns them: the same terminal code A2-C7 assigns to the
#: other direction.  A deficit epoch is recorded with its reason and is not
#: scored, not deleted, and not re-ranked.
RANKING_INPUT_DEFICIT_VERDICT = RUN_INVALID_INPUT_EVIDENCE


# --- A2-C8 (OD-26 Job B): pre-registered sample readiness protocol -------

#: Registered *before* any sample measurement (Job B).  Nothing here may be
#: tuned after a completeness result is read; that is exactly the sequencing
#: violation this clause exists to make impossible, and the enforcement is a
#: recomputation, not a re-declaration: ``sample_epoch_open_times`` is a pure
#: function of these three literals plus the quarter key, so a report's
#: claimed sample either reproduces byte-for-byte or it is rejected.
SAMPLE_SEED = 26
SAMPLE_EPOCHS_PER_QUARTER = 12
#: UTC calendar quarters (Jan-Mar/Apr-Jun/Jul-Sep/Oct-Dec), clipped to
#: ``[JUDGMENT_START, JUDGMENT_END)``.  This is the standard meaning of
#: "quarter"; it is not anchored to ``JUDGMENT_START`` and it is not a
#: window-length division, so the boundaries are checkable against a
#: calendar without reference to this corpus at all.
SAMPLE_QUARTER_DEFINITION = "utc_calendar_quarter_clipped_to_judgment_window"
#: The two dimensions checked for each sampled epoch's top-3 pool.  Outcome
#: evidence (A2-C4) and lifecycle/gap evidence (A2-C6/A2-C7) are unchanged by
#: this clause; it narrows *which epochs* get audited, not what "complete"
#: means for kline/premium-index rows.
SAMPLE_COMPLETENESS_DIMENSIONS: tuple[str, ...] = ("kline_4h", "premium_index_4h")

#: The sample-readiness verdict is a closed two-way choice.  A1's own
#: ``UNDETERMINED`` precedent is not reused here: §26차 rules that for this
#: specific sample-adequacy question there is no third outcome, because the
#: measurement either found the sample intact or it did not.
READY = "READY"
NOT_READY = "NOT_READY"
SAMPLE_VERDICTS: tuple[str, ...] = (READY, NOT_READY)

#: The frozen rule record a sample-readiness report must reproduce exactly.
SAMPLE_RULE: MappingProxyType[str, object] = MappingProxyType(
    {
        "seed": SAMPLE_SEED,
        "epochs_per_quarter": SAMPLE_EPOCHS_PER_QUARTER,
        "quarter_definition": SAMPLE_QUARTER_DEFINITION,
        "selection_algorithm": (
            "sha256(f'{seed}:{quarter_key}:{epoch_open_time_ms}') ascending, "
            "take the first epochs_per_quarter (or all, if fewer exist)"
        ),
        "completeness_dimensions": SAMPLE_COMPLETENESS_DIMENSIONS,
    }
)


def _quarter_start(year: int, quarter: int) -> datetime:
    return _utc(year, (quarter - 1) * 3 + 1, 1)


def quarter_windows() -> tuple[tuple[datetime, datetime], ...]:
    """UTC calendar-quarter windows intersecting the judgment window.

    Each window is half-open ``[start, end)`` and clipped to
    ``[JUDGMENT_START, JUDGMENT_END)``; the first and last windows are
    typically partial quarters. Order is chronological.
    """
    out: list[tuple[datetime, datetime]] = []
    year, quarter = JUDGMENT_START.year, (JUDGMENT_START.month - 1) // 3 + 1
    while True:
        start = _quarter_start(year, quarter)
        year, quarter = (year, quarter + 1) if quarter < 4 else (year + 1, 1)
        end = _quarter_start(year, quarter)
        if start >= JUDGMENT_END:
            break
        clipped_start = max(start, JUDGMENT_START)
        clipped_end = min(end, JUDGMENT_END)
        out.append((clipped_start, clipped_end))
        if end >= JUDGMENT_END:
            break
    return tuple(out)


def quarter_key(window: tuple[datetime, datetime]) -> str:
    """Deterministic label for a quarter window, e.g. ``"2021Q2"``."""
    start = window[0]
    return f"{start.year}Q{(start.month - 1) // 3 + 1}"


def sample_epoch_open_times(
    quarter_start: datetime, quarter_end: datetime, key: str
) -> tuple[int, ...]:
    """Deterministically select up to ``SAMPLE_EPOCHS_PER_QUARTER`` epochs.

    Every 4h epoch open-time in ``[quarter_start, quarter_end)`` is ranked by
    ``sha256(f"{SAMPLE_SEED}:{key}:{epoch_ms}")`` and the smallest
    ``SAMPLE_EPOCHS_PER_QUARTER`` are kept, sorted ascending. SHA-256 is a pure
    function of these inputs, so the result is stable across processes,
    machines and Python versions — no PRNG state to pin.
    """
    start_ms = int(quarter_start.timestamp() * 1000)
    end_ms = int(quarter_end.timestamp() * 1000)
    all_epochs = list(range(start_ms, end_ms, BAR_INTERVAL_MS))

    def _rank(epoch_ms: int) -> str:
        digest = f"{SAMPLE_SEED}:{key}:{epoch_ms}".encode()
        return hashlib.sha256(digest).hexdigest()

    chosen = sorted(all_epochs, key=_rank)[:SAMPLE_EPOCHS_PER_QUARTER]
    return tuple(sorted(chosen))


def sample_plan() -> MappingProxyType[str, tuple[int, ...]]:
    """The full pre-registered sample: quarter key -> chosen epoch open-times."""
    plan = {}
    for window in quarter_windows():
        key = quarter_key(window)
        plan[key] = sample_epoch_open_times(window[0], window[1], key)
    return MappingProxyType(plan)


CLAUSE_SOURCES: MappingProxyType[str, str] = MappingProxyType(
    {
        "A2-C1": "NW-F2",
        "A2-C2": "NW-F5",
        "A2-C3": "NW-F5",
        "A2-C4": "NW-F4",
        "A2-C5": "NW-F6",
        "A2-C6": "OD-26",
        "A2-C7": "OD-26",
        "A2-C8": "OD-26-JOB-B",
        "A2-C9": "OD-31",
    }
)
