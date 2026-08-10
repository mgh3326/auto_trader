"""Golden rejections for the ``DFC_V22_RESEARCH_MIN`` (A2) contract.

The point of these tests is not that a well-formed corpus validates — that is the
easy half, and it is covered once by ``test_baseline_artifacts_validate``.  The
point is that each *specific* bad input the contract names is actually shot down,
with the right terminal code and for the right reason.  Every case therefore
asserts the wording too: a validator that rejects an input for an unrelated
reason is not enforcing the clause it was supposed to enforce.

Network-zero by construction: everything here is built in memory.
"""

from __future__ import annotations

import ast
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from research.dfc_v22_research_min import contract as c
from research.dfc_v22_research_min import validation as v
from research.dfc_v22_research_min.schema import (
    CANONICAL_TABLES,
    KLINES_4H_SCHEMA,
    OUTCOMES_SCHEMA,
    PIT_UNIVERSE_SCHEMA,
    PREMIUM_INDEX_4H_SCHEMA,
    PREMIUM_INDEX_GAP_AUDIT_SCHEMA,
)

GOLDEN_PATH = Path(__file__).parent / "golden" / "violation_cases.json"

T0 = int(datetime(2021, 5, 2, tzinfo=UTC).timestamp() * 1000)
T1 = T0 + c.BAR_INTERVAL_MS
BAR = c.BAR_INTERVAL_MS


# --------------------------------------------------------------------------
# baseline artifacts
# --------------------------------------------------------------------------


def _provenance(payload_sha: str) -> dict[str, Any]:
    return {
        "endpoint": "https://fapi.binance.com/fapi/v1/klines",
        "query": "symbol=XRPUSDT&interval=4h&startTime=...",
        "retrieved_at": datetime(2026, 8, 9, 17, 0, tzinfo=UTC),
        "source_schema": "binance-usdm-kline-v1",
        "object_sha256": "0" * 64,
        "payload_sha256": payload_sha,
    }


def _kline_row(symbol: str, open_time: int, close: str, payload: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "open_time": open_time,
        "open": "1.0000",
        "high": "1.0000",
        "low": "1.0000",
        "close": close,
        "volume": "10.0",
        "close_time": open_time + BAR - 1,
        "quote_asset_volume": "10.0",
        "number_of_trades": 7,
        "taker_buy_base_asset_volume": "5.0",
        "taker_buy_quote_asset_volume": "5.0",
        "ignore": "0",
        **_provenance(payload),
    }


#: Four bars: the two signal epochs and their two immediately-next bars.
_KLINE_ROWS: list[dict[str, Any]] = [
    _kline_row("XRPUSDT", T0, "1.0000", "p-xrp-t0"),
    _kline_row("XRPUSDT", T0 + BAR, "1.0100", "p-xrp-t0n"),
    _kline_row("SOLUSDT", T1, "2.0000", "p-sol-t1"),
    _kline_row("SOLUSDT", T1 + BAR, "1.9800", "p-sol-t1n"),
]

#: One epoch on each arm, so the baseline exercises both admissible labels.
_DECISIONS: list[dict[str, Any]] = [
    {
        "signal_epoch_open_time": T0,
        "candidate_any": c.ARM_CANDIDATE,
        "winner_symbol": "XRPUSDT",
    },
    {
        "signal_epoch_open_time": T1,
        "candidate_any": c.ARM_CONTROL,
        "winner_symbol": "SOLUSDT",
    },
]


def _bps(start: str, end: str) -> float:
    return abs(math.log(float(end) / float(start))) * 10_000.0


def _outcome_row(
    epoch: int,
    symbol: str,
    t_payload: str,
    next_payload: str,
    t_close: str,
    next_close: str,
    arm: str,
) -> dict[str, Any]:
    return {
        "signal_epoch_open_time": epoch,
        "signal_epoch_close_time": epoch + BAR - 1,
        "candidate_any": arm,
        "winner_symbol": symbol,
        "t_kline_open_time": epoch,
        "t_close_payload_sha256": t_payload,
        "next_kline_open_time": epoch + BAR,
        "next_kline_close_time": epoch + BAR + BAR - 1,
        "next_close_payload_sha256": next_payload,
        "outcome_abs_log_return_bps": _bps(t_close, next_close),
        "evidence_status": c.OUTCOME_EVIDENCE_STATUS,
    }


_OUTCOME_ROWS: list[dict[str, Any]] = [
    _outcome_row(
        T0, "XRPUSDT", "p-xrp-t0", "p-xrp-t0n", "1.0000", "1.0100", c.ARM_CANDIDATE
    ),
    _outcome_row(
        T1, "SOLUSDT", "p-sol-t1", "p-sol-t1n", "2.0000", "1.9800", c.ARM_CONTROL
    ),
]


def klines(rows: list[dict[str, Any]] | None = None) -> pa.Table:
    return pa.Table.from_pylist(rows or _KLINE_ROWS, schema=KLINES_4H_SCHEMA)


def outcomes(
    rows: list[dict[str, Any]] | None = None,
    *,
    extra: tuple[str, pa.DataType, list[Any]] | None = None,
) -> pa.Table:
    rows = rows if rows is not None else _OUTCOME_ROWS
    table = pa.Table.from_pylist(rows, schema=OUTCOMES_SCHEMA)
    if extra is not None:
        name, dtype, values = extra
        table = table.append_column(
            pa.field(name, dtype, nullable=False), pa.array(values, type=dtype)
        )
    return table


def _source(kind: str, **overrides: Any) -> dict[str, Any]:
    source = {
        "name": f"{kind}-object",
        "kind": kind,
        "endpoint": f"https://fapi.binance.com/fapi/v1/{kind}",
        "query": "symbol=XRPUSDT&interval=4h",
        "retrieved_at": "2026-08-09T17:00:00Z",
        "schema": f"binance-{kind}-v1",
        "object_kind": "http_response",
        "object_sha256": "a" * 64,
        "payload_sha256_by_epoch": {str(T0): "b" * 64},
        "byte_size": 4096,
        "row_count": 4,
    }
    source.update(overrides)
    return source


#: One gap symbol per disposition: audited per epoch, and one excluded under
#: each of the two exclusion reasons that need no base symbol / need one.
GAP_SYMBOLS = ("GAPUSDT", "BTCUSDT_210625", "LEGACYUSDTSETTLED")
IN_WINDOW_GAP_SYMBOLS = ("GAPUSDT",)


def lifecycle_eligibility(**overrides: Any) -> dict[str, Any]:
    declared: dict[str, Any] = {
        "authoritative_public_source": None,
        "evidence_kind": c.ELIGIBILITY_EVIDENCE_KIND,
        "proxy_limits": dict(c.LIFECYCLE_PROXY_LIMITS),
    }
    declared.update(overrides)
    return declared


def premium_index_gap(**overrides: Any) -> dict[str, Any]:
    gap: dict[str, Any] = {
        "symbols": list(GAP_SYMBOLS),
        "in_window_symbols": list(IN_WINDOW_GAP_SYMBOLS),
        "exclusions": [
            {
                "symbol": "BTCUSDT_210625",
                "reason": "not_perpetual",
                "evidence_sha256": "1" * 64,
            },
            {
                "symbol": "LEGACYUSDTSETTLED",
                "reason": "same_instrument_as_base",
                "base_symbol": "LEGACYUSDT",
                "evidence_sha256": "2" * 64,
            },
        ],
        "listing_endpoint": (
            "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
        ),
        "listing_retrieved_at": "2026-08-10T05:00:00Z",
        "measurement_sha256": "3" * 64,
    }
    gap.update(overrides)
    return gap


#: A2-C9.  The baseline restates the frozen enumeration exactly, so every
#: mutant below is a *single* deviation from a manifest that otherwise passes.
def ranking_input_deficit(**overrides: Any) -> dict[str, Any]:
    section: dict[str, Any] = {
        "enumeration_path": c.RANKING_INPUT_DEFICIT_ENUMERATION_PATH,
        "enumeration_sha256": c.RANKING_INPUT_DEFICIT_ENUMERATION_SHA256,
        "epochs": list(c.RANKING_INPUT_DEFICIT_EPOCHS),
        "verdict": c.RANKING_INPUT_DEFICIT_VERDICT,
        "rows": [
            {
                "epoch_open_time": epoch,
                "as_archived_rank3": as_archived,
                "would_have_been_rank3": would_have_been,
            }
            for epoch, as_archived, would_have_been in c.RANKING_INPUT_DEFICIT_ROWS
        ],
    }
    section.update(overrides)
    return section


#: The pool the corpus must record at a deficit epoch: the ranking the archive's
#: own (short) input produces.  ``DEFICIT_EPOCH`` is the first enumerated one.
DEFICIT_EPOCH = c.RANKING_INPUT_DEFICIT_EPOCHS[0]
DEFICIT_AS_ARCHIVED = (
    *c.RANKING_INPUT_DEFICIT_UNCHANGED_HEAD,
    c.RANKING_INPUT_DEFICIT_ROWS[0][1],
)
DEFICIT_WOULD_HAVE_BEEN = (
    *c.RANKING_INPUT_DEFICIT_UNCHANGED_HEAD,
    c.RANKING_INPUT_DEFICIT_ROWS[0][2],
)


def deficit_pool_rows(
    symbols: tuple[str, ...] = DEFICIT_AS_ARCHIVED,
) -> list[dict[str, Any]]:
    """A three-rank pool at the first enumerated deficit epoch."""
    return [
        {
            **pit_universe_row(rank, symbol, str(300 - 100 * (rank - 1)) + ".0"),
            "epoch_open_time": DEFICIT_EPOCH,
        }
        for rank, symbol in enumerate(symbols, start=1)
    ]


def manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contract_id": c.CONTRACT_ID,
        "contract_doc_sha256": "c" * 64,
        "corpus_id": c.CORPUS_ID,
        "root": c.CORPUS_ROOT,
        "frozen": True,
        "warmup": {"start": "2021-02-02T00:00:00Z", "end": "2021-05-02T00:00:00Z"},
        "judgment_window": {
            "start": "2021-05-02T00:00:00Z",
            "end": "2023-08-04T00:00:00Z",
        },
        "outcome_tail_bars": c.OUTCOME_TAIL_BARS,
        "universe": {
            "lookback_calendar_days": c.UNIVERSE_LOOKBACK_CALENDAR_DAYS,
            "ranking_metric": c.UNIVERSE_RANKING_METRIC,
            "top_n": c.UNIVERSE_TOP_N,
            "tie_break": c.UNIVERSE_TIE_BREAK,
            "instrument_class": c.UNIVERSE_INSTRUMENT_CLASS,
        },
        "imputation": {"policy": "forbidden", "imputed_row_count": 0},
        "prerequisite_readiness": {
            "contract_id": c.CONTRACT_ID,
            "dimensions": list(c.A2_JUDGED_DIMENSIONS),
        },
        "lifecycle_eligibility": lifecycle_eligibility(),
        "premium_index_gap": premium_index_gap(),
        "ranking_input_deficit": ranking_input_deficit(),
        "sources": [_source(kind) for kind in c.REQUIRED_SOURCE_KINDS],
        "tables": [
            {
                "name": name,
                "path": f"canonical/{name}.parquet",
                "sha256": "d" * 64,
                "row_count": 4,
                "byte_size": 2048,
            }
            for name in (
                "klines_4h",
                "premium_index_4h",
                "pit_universe",
                "premium_index_gap_audit",
                "outcomes",
            )
        ],
        "admissibility": {
            "basis": c.ADMISSIBILITY_BASIS,
            "independent_recollection_required": True,
            "comparison_keys": list(c.ADMISSIBILITY_COMPARISON_KEYS),
        },
    }
    base.update(overrides)
    return base


def pit_universe_row(
    rank: int = 1,
    symbol: str = "XRPUSDT",
    quote_volume: str = "123456.7",
) -> dict[str, Any]:
    return {
        "epoch_open_time": T0,
        "rank": rank,
        "symbol": symbol,
        "quote_volume_lookback": quote_volume,
        "lookback_start_time": T0 - 30 * 24 * 60 * 60 * 1000,
        "lookback_end_time": T0,
        "lifecycle_evidence_sha256": "e" * 64,
        "eligibility_evidence_sha256": "f" * 64,
        **_provenance("p-univ-t0"),
    }


#: A full pool: three ranks, strictly descending quote volume.  The rank-3 row
#: is the cut every gap symbol is measured against.
_POOL_ROWS: list[dict[str, Any]] = [
    pit_universe_row(1, "XRPUSDT", "300.0"),
    pit_universe_row(2, "SOLUSDT", "200.0"),
    pit_universe_row(3, "ADAUSDT", "100.0"),
]


def pit_universe(rows: list[dict[str, Any]] | None = None) -> pa.Table:
    return pa.Table.from_pylist(rows or _POOL_ROWS, schema=PIT_UNIVERSE_SCHEMA)


def gap_audit_row(
    symbol: str = "GAPUSDT",
    *,
    status: str = c.ELIGIBLE,
    quote_volume: str | None = "50.0",
    verdict: str = c.NO_IMPACT,
    epoch: int = T0,
) -> dict[str, Any]:
    return {
        "epoch_open_time": epoch,
        "symbol": symbol,
        "eligibility_status": status,
        "eligibility_evidence_sha256": "9" * 64,
        "quote_volume_lookback": quote_volume,
        "verdict": verdict,
        **_provenance("p-gap-t0"),
    }


def premium_index(rows: list[dict[str, Any]] | None = None) -> pa.Table:
    rows = (
        rows
        if rows is not None
        else [
            {
                "symbol": row["symbol"],
                "open_time": row["open_time"],
                "close_time": row["close_time"],
                "premium_index_close": "0.00012",
                **_provenance(f"pi-{row['payload_sha256']}"),
            }
            for row in _KLINE_ROWS
        ]
    )
    return pa.Table.from_pylist(rows, schema=PREMIUM_INDEX_4H_SCHEMA)


def gap_audit(rows: list[dict[str, Any]] | None = None) -> pa.Table:
    rows = rows if rows is not None else [gap_audit_row()]
    return pa.Table.from_pylist(rows, schema=PREMIUM_INDEX_GAP_AUDIT_SCHEMA)


# --------------------------------------------------------------------------
# case builders — each one feeds exactly one bad artifact to a validator
# --------------------------------------------------------------------------


def _case_outcomes_free_bool_column() -> None:
    v.validate_outcomes(
        outcomes(extra=("next_bar_present", pa.bool_(), [True, True])),
        klines(),
        _DECISIONS,
    )


def _case_outcomes_free_price_column() -> None:
    v.validate_outcomes(
        outcomes(extra=("entry_price", pa.float64(), [1.0, 2.0])),
        klines(),
        _DECISIONS,
    )


def _case_outcomes_unknown_column() -> None:
    v.validate_outcomes(
        outcomes(extra=("analyst_note", pa.string(), ["ok", "ok"])),
        klines(),
        _DECISIONS,
    )


def _case_outcomes_row_deleted_for_missing_next_bar() -> None:
    # The second decision's next bar is unavailable, so the builder "solved" it
    # by dropping the row.  That is precisely what NW-F4 forbids.
    v.validate_outcomes(outcomes(_OUTCOME_ROWS[:1]), klines(), _DECISIONS)


def _case_outcomes_next_bar_absent_from_corpus() -> None:
    truncated = [r for r in _KLINE_ROWS if not (r["open_time"] == T1 + BAR)]
    v.validate_outcomes(outcomes(), klines(truncated), _DECISIONS)


def _case_outcomes_next_bar_not_immediately_next() -> None:
    rows = [dict(r) for r in _OUTCOME_ROWS]
    rows[0]["next_kline_open_time"] = T0 + 2 * BAR
    v.validate_outcomes(outcomes(rows), klines(), _DECISIONS)


def _case_outcomes_hand_written_return() -> None:
    rows = [dict(r) for r in _OUTCOME_ROWS]
    rows[0]["outcome_abs_log_return_bps"] = 250.0
    v.validate_outcomes(outcomes(rows), klines(), _DECISIONS)


def _case_outcomes_close_payload_hash_mismatch() -> None:
    rows = [dict(r) for r in _OUTCOME_ROWS]
    rows[0]["t_close_payload_sha256"] = "p-not-the-bar"
    v.validate_outcomes(outcomes(rows), klines(), _DECISIONS)


def _case_outcomes_arm_label_not_from_decision() -> None:
    # An *admissible* label, but not the one this decision carries: the row was
    # relabelled from candidate to control by hand.
    rows = [dict(r) for r in _OUTCOME_ROWS]
    rows[0]["candidate_any"] = c.ARM_CONTROL
    v.validate_outcomes(outcomes(rows), klines(), _DECISIONS)


def _case_outcomes_arbitrary_string_arm_label() -> None:
    # The v1 defect: an arm value outside the contract's label set was
    # admissible on this side while the v2.2 side silently coerced it to True.
    # Decision and row agree, so only the closed domain can shoot it down.
    decisions = [dict(d) for d in _DECISIONS]
    decisions[0]["candidate_any"] = "arbitrary-nonboolean-arm"
    rows = [dict(r) for r in _OUTCOME_ROWS]
    rows[0]["candidate_any"] = "arbitrary-nonboolean-arm"
    v.validate_outcomes(outcomes(rows), klines(), decisions)


def _case_outcomes_row_arm_label_outside_domain() -> None:
    # Same defect reached from the row side only.
    rows = [dict(r) for r in _OUTCOME_ROWS]
    rows[0]["candidate_any"] = "arm_dfc_2c_4h"
    v.validate_outcomes(outcomes(rows), klines(), _DECISIONS)


def _case_outcomes_free_bool_arm_label() -> None:
    # A truth value is not an arm label.  This is the shape a ``bool(...)``
    # coercion produces, so it must fail before any comparison.
    decisions = [dict(d) for d in _DECISIONS]
    decisions[0]["candidate_any"] = True
    v.validate_outcomes(outcomes(), klines(), decisions)


def _case_manifest_funding_source_kind() -> None:
    sources = [_source(kind) for kind in c.REQUIRED_SOURCE_KINDS]
    sources.append(
        _source("funding_rate", name="extra-object", endpoint="https://x/fr")
    )
    v.validate_manifest(manifest(sources=sources))


def _case_manifest_open_interest_endpoint() -> None:
    sources = [_source(kind) for kind in c.REQUIRED_SOURCE_KINDS]
    sources[0] = _source(
        "usdm_kline_4h",
        name="oi-relabelled-as-kline",
        endpoint="https://fapi.binance.com/futures/data/openInterestHist",
    )
    v.validate_manifest(manifest(sources=sources))


def _case_table_klines_open_interest_column() -> None:
    rows = [dict(r) for r in _KLINE_ROWS]
    for row in rows:
        row["open_interest"] = "42.0"
    fields = list(KLINES_4H_SCHEMA) + [
        pa.field("open_interest", pa.string(), nullable=False)
    ]
    v.validate_table("klines_4h", pa.Table.from_pylist(rows, schema=pa.schema(fields)))


def _case_table_klines_missing_ignore_field() -> None:
    rows = [{k: val for k, val in r.items() if k != "ignore"} for r in _KLINE_ROWS]
    fields = [f for f in KLINES_4H_SCHEMA if f.name != "ignore"]
    v.validate_table("klines_4h", pa.Table.from_pylist(rows, schema=pa.schema(fields)))


def _case_table_klines_float_close() -> None:
    rows = [dict(r) for r in _KLINE_ROWS]
    for row in rows:
        row["close"] = float(row["close"])
    fields = [
        pa.field("close", pa.float64(), nullable=False) if f.name == "close" else f
        for f in KLINES_4H_SCHEMA
    ]
    v.validate_table("klines_4h", pa.Table.from_pylist(rows, schema=pa.schema(fields)))


def _case_table_pit_universe_free_bool_eligible() -> None:
    row = pit_universe_row()
    row["eligible"] = True
    fields = list(PIT_UNIVERSE_SCHEMA) + [
        pa.field("eligible", pa.bool_(), nullable=False)
    ]
    v.validate_table(
        "pit_universe", pa.Table.from_pylist([row], schema=pa.schema(fields))
    )


def _case_manifest_missing_object_sha256() -> None:
    sources = [_source(kind) for kind in c.REQUIRED_SOURCE_KINDS]
    sources[0] = {k: val for k, val in sources[0].items() if k != "object_sha256"}
    v.validate_manifest(manifest(sources=sources))


def _case_manifest_missing_retrieved_at() -> None:
    sources = [_source(kind) for kind in c.REQUIRED_SOURCE_KINDS]
    sources[0] = {k: val for k, val in sources[0].items() if k != "retrieved_at"}
    v.validate_manifest(manifest(sources=sources))


def _case_manifest_payload_sha_not_keyed_by_epoch() -> None:
    # A bare list of hashes loses the epoch binding, so no verifier can tell
    # which 4h payload a hash belongs to.
    sources = [_source(kind) for kind in c.REQUIRED_SOURCE_KINDS]
    sources[0] = _source(sources[0]["kind"], payload_sha256_by_epoch=["b" * 64])
    v.validate_manifest(manifest(sources=sources))


def _case_manifest_table_missing_freeze_sha() -> None:
    tables = [dict(t) for t in manifest()["tables"]]
    tables[0] = {k: val for k, val in tables[0].items() if k != "sha256"}
    v.validate_manifest(manifest(tables=tables))


def _case_manifest_self_consistency_admissibility() -> None:
    v.validate_manifest(
        manifest(
            admissibility={
                "basis": "self_consistency",
                "independent_recollection_required": False,
                "comparison_keys": list(c.ADMISSIBILITY_COMPARISON_KEYS),
            }
        )
    )


def _case_manifest_not_frozen() -> None:
    v.validate_manifest(manifest(frozen=False))


def _case_manifest_a1_as_prerequisite() -> None:
    v.validate_manifest(
        manifest(
            prerequisite_readiness={
                "contract_id": "FUT-DATA-A1",
                "dimensions": list(c.A2_JUDGED_DIMENSIONS),
            }
        )
    )


def _case_manifest_scratch_root() -> None:
    v.validate_manifest(manifest(root="/private/tmp/dfc-a1-scratch/"))


def _case_manifest_widened_judgment_window() -> None:
    v.validate_manifest(
        manifest(
            judgment_window={
                "start": "2021-05-02T00:00:00Z",
                "end": "2024-08-04T00:00:00Z",
            }
        )
    )


def _case_manifest_universe_top_five() -> None:
    universe = dict(manifest()["universe"])
    universe["top_n"] = 5
    v.validate_manifest(manifest(universe=universe))


def _case_manifest_imputed_rows_present() -> None:
    v.validate_manifest(
        manifest(imputation={"policy": "forbidden", "imputed_row_count": 12})
    )


def _case_manifest_missing_required_source_kind() -> None:
    sources = [
        _source(kind) for kind in c.REQUIRED_SOURCE_KINDS if kind != "premium_index_4h"
    ]
    v.validate_manifest(manifest(sources=sources))


# --- A2-C6: lifecycle authority absence ------------------------------------


def _case_lifecycle_authority_invented() -> None:
    # The endpoint does not publish lifecycle state; naming it asserts an
    # authority A2-MEASURE looked for and did not find.
    v.validate_manifest(
        manifest(
            lifecycle_eligibility=lifecycle_eligibility(
                authoritative_public_source="GET /fapi/v1/exchangeInfo"
            )
        )
    )


def _case_lifecycle_proxy_promoted_to_evidence() -> None:
    v.validate_manifest(
        manifest(
            lifecycle_eligibility=lifecycle_eligibility(
                evidence_kind="exchange_info_onboard_date"
            )
        )
    )


def _case_lifecycle_proxy_limit_softened() -> None:
    limits = dict(c.LIFECYCLE_PROXY_LIMITS)
    limits["archive_month_range"] = "effectively equivalent to a lifecycle record"
    v.validate_manifest(
        manifest(lifecycle_eligibility=lifecycle_eligibility(proxy_limits=limits))
    )


def _case_gap_symbol_missing_eligibility_evidence() -> None:
    row = gap_audit_row()
    row["eligibility_evidence_sha256"] = ""
    v.validate_premium_index_gap(
        gap_audit([row]), pit_universe(), IN_WINDOW_GAP_SYMBOLS
    )


# --- A2-C7: the archive gap and the per-epoch verdict -----------------------


def _case_gap_symbol_unaccounted_for() -> None:
    # Present in the measured diff, neither audited nor excluded: the silent
    # disappearance the closed reason set exists to prevent.
    gap = premium_index_gap(exclusions=premium_index_gap()["exclusions"][:1])
    v.validate_manifest(manifest(premium_index_gap=gap))


def _case_gap_exclusion_unknown_reason() -> None:
    exclusions = [dict(e) for e in premium_index_gap()["exclusions"]]
    exclusions[0]["reason"] = "not_relevant"
    v.validate_manifest(
        manifest(premium_index_gap=premium_index_gap(exclusions=exclusions))
    )


def _case_gap_exclusion_base_symbol_also_in_gap() -> None:
    # "Same instrument as base" is worthless if the base has no premium index
    # either — it just moves the gap one symbol along.
    exclusions = [dict(e) for e in premium_index_gap()["exclusions"]]
    exclusions[1]["base_symbol"] = "GAPUSDT"
    v.validate_manifest(
        manifest(premium_index_gap=premium_index_gap(exclusions=exclusions))
    )


def _case_gap_symbol_inside_candidate_pool() -> None:
    pool = [dict(r) for r in _POOL_ROWS]
    pool[2]["symbol"] = "GAPUSDT"
    v.validate_premium_index_gap(
        gap_audit([gap_audit_row(quote_volume="100.0")]),
        pit_universe(pool),
        IN_WINDOW_GAP_SYMBOLS,
    )


def _case_gap_epoch_audit_row_omitted() -> None:
    v.validate_premium_index_gap(gap_audit([]), pit_universe(), IN_WINDOW_GAP_SYMBOLS)


def _case_gap_outranking_symbol_reported_no_impact() -> None:
    # MUTANT ②: the gap symbol beats the rank-3 cut, but the epoch is written
    # up as clean.  The verdict is recomputed, so the claim does not survive.
    v.validate_premium_index_gap(
        gap_audit([gap_audit_row(quote_volume="250.0", verdict=c.NO_IMPACT)]),
        pit_universe(),
        IN_WINDOW_GAP_SYMBOLS,
    )


def _case_gap_outranking_symbol_silently_reranked() -> None:
    # MUTANT ①: the gap symbol outranks the pool and the builder resolved it by
    # promoting the next-ranked symbol into the vacancy, then recording the
    # honest verdict for the symbol it dropped.  The epoch must still be
    # RUN_INVALID_INPUT_EVIDENCE — a re-ranked pool is not a repair.
    v.validate_premium_index_gap(
        gap_audit(
            [gap_audit_row(quote_volume="250.0", verdict=c.RUN_INVALID_INPUT_EVIDENCE)]
        ),
        pit_universe(),
        IN_WINDOW_GAP_SYMBOLS,
    )


def _case_gap_pool_order_is_not_the_ranking() -> None:
    # The pool was re-ordered so the promoted symbol looks like rank 3.
    pool = [dict(r) for r in _POOL_ROWS]
    pool[1]["quote_volume_lookback"] = "50.0"
    v.validate_premium_index_gap(gap_audit(), pit_universe(pool), IN_WINDOW_GAP_SYMBOLS)


def _case_gap_pool_shorter_than_top_n() -> None:
    v.validate_premium_index_gap(
        gap_audit(), pit_universe(_POOL_ROWS[:2]), IN_WINDOW_GAP_SYMBOLS
    )


def _case_gap_not_eligible_row_carries_volume() -> None:
    v.validate_premium_index_gap(
        gap_audit([gap_audit_row(status=c.NOT_ELIGIBLE, quote_volume="10.0")]),
        pit_universe(),
        IN_WINDOW_GAP_SYMBOLS,
    )


def _case_gap_declaration_absent() -> None:
    base = manifest()
    del base["premium_index_gap"]
    v.validate_manifest(base)


# --------------------------------------------------------------------------
# A2-C8: pre-registered sample readiness protocol
# --------------------------------------------------------------------------


def _sample_report(**overrides: Any) -> dict[str, Any]:
    plan = c.sample_plan()
    first_quarter = next(iter(plan))
    first_epoch = plan[first_quarter][0]
    base: dict[str, Any] = {
        "contract_id": c.CONTRACT_ID,
        "contract_doc_sha256": "0" * 64,
        "sample_rule": dict(c.SAMPLE_RULE),
        "quarters": {k: list(v_) for k, v_ in plan.items()},
        "rows": [
            {
                "quarter": first_quarter,
                "epoch_open_time": first_epoch,
                "rank": 1,
                "symbol": "BTCUSDT",
                "kline_complete": True,
                "premium_index_complete": True,
                "missing_detail": None,
                "provenance_sha256": "a" * 64,
            }
        ],
        "run_invalid_epochs": [],
        "verdict": c.READY,
        "measured_at": "2026-08-10T00:00:00Z",
    }
    base.update(overrides)
    return base


def _case_sample_verdict_undetermined() -> None:
    v.validate_sample_readiness(_sample_report(verdict="UNDETERMINED"))


def _case_sample_rule_tampered() -> None:
    tampered = dict(c.SAMPLE_RULE)
    tampered["seed"] = 99
    v.validate_sample_readiness(_sample_report(sample_rule=tampered))


def _case_sample_verdict_not_recomputed() -> None:
    report = _sample_report()
    report["rows"][0]["kline_complete"] = False
    report["rows"][0]["missing_detail"] = "kline absent"
    # verdict is left as READY even though the only row is incomplete.
    v.validate_sample_readiness(report)


def _case_sample_epoch_outside_registered_plan() -> None:
    report = _sample_report()
    report["rows"][0]["epoch_open_time"] = report["rows"][0]["epoch_open_time"] + 1
    v.validate_sample_readiness(report)


# --- A2-C9 (OD-31) — the three mutants the amendment must shoot down --------


def _deficit_decisions(epoch: int) -> list[dict[str, Any]]:
    return [
        {
            "signal_epoch_open_time": epoch,
            "candidate_any": c.ARM_CANDIDATE,
            "winner_symbol": DEFICIT_AS_ARCHIVED[0],
        }
    ]


def _case_deficit_epoch_processed_normally() -> None:
    # MUTANT ①: an enumerated epoch is scored as if its ranking input had been
    # complete.  Everything else about the corpus is well-formed, so only the
    # terminal-code check can catch it.
    v.validate_ranking_input_deficit(
        manifest(),
        pit_universe(deficit_pool_rows()),
        None,
        _deficit_decisions(DEFICIT_EPOCH),
    )


def _case_deficit_epoch_scored_in_outcomes() -> None:
    # Same mutant reached from the outcomes table rather than the decision set:
    # a builder that never registered the decision but still emitted a row.
    rows = [
        _outcome_row(
            DEFICIT_EPOCH,
            "XRPUSDT",
            "p-xrp-t0",
            "p-xrp-t0n",
            "1.0000",
            "1.0100",
            c.ARM_CANDIDATE,
        )
    ]
    v.validate_ranking_input_deficit(
        manifest(), pit_universe(deficit_pool_rows()), outcomes(rows), ()
    )


def _case_deficit_epoch_silently_reranked() -> None:
    # MUTANT ②: the pool records the ranking *complete* input would have
    # produced.  It looks more accurate, and it is exactly what §31차 forbids —
    # no reader can reproduce it from the evidence the corpus carries.
    v.validate_ranking_input_deficit(
        manifest(), pit_universe(deficit_pool_rows(DEFICIT_WOULD_HAVE_BEEN)), None, ()
    )


def _case_deficit_enumeration_list_changed() -> None:
    # MUTANT ③: the enumeration file moved after the list was pre-registered.
    # The manifest is internally consistent; only the pinned digest exposes it.
    v.validate_manifest(
        manifest(
            ranking_input_deficit=ranking_input_deficit(
                enumeration_sha256="9" * 64,
            )
        )
    )


def _case_deficit_epochs_narrowed() -> None:
    # The same edit reached from the list itself: drop the inconvenient epochs
    # and re-hash nothing.
    v.validate_manifest(
        manifest(
            ranking_input_deficit=ranking_input_deficit(
                epochs=list(c.RANKING_INPUT_DEFICIT_EPOCHS[:-1]),
            )
        )
    )


def _case_deficit_row_disagrees_with_enumeration() -> None:
    rows = [dict(r) for r in ranking_input_deficit()["rows"]]
    rows[0]["would_have_been_rank3"] = rows[0]["as_archived_rank3"]
    v.validate_manifest(
        manifest(ranking_input_deficit=ranking_input_deficit(rows=rows))
    )


def _case_deficit_epoch_row_deleted() -> None:
    # Deleting the epoch is how a deficit disappears without leaving a trace.
    # The first and last enumerated epochs are kept so the table's covered range
    # still spans the whole enumeration — the deletion is an interior hole,
    # which is the only shape this can actually take.
    kept: list[dict[str, Any]] = []
    for epoch, as_archived, _ in (
        c.RANKING_INPUT_DEFICIT_ROWS[0],
        c.RANKING_INPUT_DEFICIT_ROWS[-1],
    ):
        symbols = (*c.RANKING_INPUT_DEFICIT_UNCHANGED_HEAD, as_archived)
        kept.extend(
            {**row, "epoch_open_time": epoch} for row in deficit_pool_rows(symbols)
        )
    v.validate_ranking_input_deficit(manifest(), pit_universe(kept), None, ())


def _case_deficit_declaration_absent() -> None:
    bare = {k: val for k, val in manifest().items() if k != "ranking_input_deficit"}
    v.validate_input_evidence(bare)


CASE_BUILDERS: dict[str, Callable[[], None]] = {
    "outcomes_free_bool_column": _case_outcomes_free_bool_column,
    "outcomes_free_price_column": _case_outcomes_free_price_column,
    "outcomes_unknown_column": _case_outcomes_unknown_column,
    "outcomes_row_deleted_for_missing_next_bar": (
        _case_outcomes_row_deleted_for_missing_next_bar
    ),
    "outcomes_next_bar_absent_from_corpus": _case_outcomes_next_bar_absent_from_corpus,
    "outcomes_next_bar_not_immediately_next": (
        _case_outcomes_next_bar_not_immediately_next
    ),
    "outcomes_hand_written_return": _case_outcomes_hand_written_return,
    "outcomes_close_payload_hash_mismatch": _case_outcomes_close_payload_hash_mismatch,
    "outcomes_arm_label_not_from_decision": _case_outcomes_arm_label_not_from_decision,
    "outcomes_arbitrary_string_arm_label": _case_outcomes_arbitrary_string_arm_label,
    "outcomes_row_arm_label_outside_domain": _case_outcomes_row_arm_label_outside_domain,
    "outcomes_free_bool_arm_label": _case_outcomes_free_bool_arm_label,
    "manifest_funding_source_kind": _case_manifest_funding_source_kind,
    "manifest_open_interest_endpoint": _case_manifest_open_interest_endpoint,
    "table_klines_open_interest_column": _case_table_klines_open_interest_column,
    "table_klines_missing_ignore_field": _case_table_klines_missing_ignore_field,
    "table_klines_float_close": _case_table_klines_float_close,
    "table_pit_universe_free_bool_eligible": _case_table_pit_universe_free_bool_eligible,
    "manifest_missing_object_sha256": _case_manifest_missing_object_sha256,
    "manifest_missing_retrieved_at": _case_manifest_missing_retrieved_at,
    "manifest_payload_sha_not_keyed_by_epoch": (
        _case_manifest_payload_sha_not_keyed_by_epoch
    ),
    "manifest_table_missing_freeze_sha": _case_manifest_table_missing_freeze_sha,
    "manifest_self_consistency_admissibility": (
        _case_manifest_self_consistency_admissibility
    ),
    "manifest_not_frozen": _case_manifest_not_frozen,
    "manifest_a1_as_prerequisite": _case_manifest_a1_as_prerequisite,
    "manifest_scratch_root": _case_manifest_scratch_root,
    "manifest_widened_judgment_window": _case_manifest_widened_judgment_window,
    "manifest_universe_top_five": _case_manifest_universe_top_five,
    "manifest_imputed_rows_present": _case_manifest_imputed_rows_present,
    "manifest_missing_required_source_kind": _case_manifest_missing_required_source_kind,
    "lifecycle_authority_invented": _case_lifecycle_authority_invented,
    "lifecycle_proxy_promoted_to_evidence": _case_lifecycle_proxy_promoted_to_evidence,
    "lifecycle_proxy_limit_softened": _case_lifecycle_proxy_limit_softened,
    "gap_symbol_missing_eligibility_evidence": (
        _case_gap_symbol_missing_eligibility_evidence
    ),
    "gap_symbol_unaccounted_for": _case_gap_symbol_unaccounted_for,
    "gap_exclusion_unknown_reason": _case_gap_exclusion_unknown_reason,
    "gap_exclusion_base_symbol_also_in_gap": _case_gap_exclusion_base_symbol_also_in_gap,
    "gap_symbol_inside_candidate_pool": _case_gap_symbol_inside_candidate_pool,
    "gap_epoch_audit_row_omitted": _case_gap_epoch_audit_row_omitted,
    "gap_outranking_symbol_reported_no_impact": (
        _case_gap_outranking_symbol_reported_no_impact
    ),
    "gap_outranking_symbol_silently_reranked": (
        _case_gap_outranking_symbol_silently_reranked
    ),
    "gap_pool_order_is_not_the_ranking": _case_gap_pool_order_is_not_the_ranking,
    "gap_pool_shorter_than_top_n": _case_gap_pool_shorter_than_top_n,
    "gap_not_eligible_row_carries_volume": _case_gap_not_eligible_row_carries_volume,
    "gap_declaration_absent": _case_gap_declaration_absent,
    "sample_verdict_undetermined": _case_sample_verdict_undetermined,
    "sample_rule_tampered": _case_sample_rule_tampered,
    "sample_verdict_not_recomputed": _case_sample_verdict_not_recomputed,
    "sample_epoch_outside_registered_plan": _case_sample_epoch_outside_registered_plan,
    "deficit_epoch_processed_normally": _case_deficit_epoch_processed_normally,
    "deficit_epoch_scored_in_outcomes": _case_deficit_epoch_scored_in_outcomes,
    "deficit_epoch_silently_reranked": _case_deficit_epoch_silently_reranked,
    "deficit_enumeration_list_changed": _case_deficit_enumeration_list_changed,
    "deficit_epochs_narrowed": _case_deficit_epochs_narrowed,
    "deficit_row_disagrees_with_enumeration": (
        _case_deficit_row_disagrees_with_enumeration
    ),
    "deficit_epoch_row_deleted": _case_deficit_epoch_row_deleted,
    "deficit_declaration_absent": _case_deficit_declaration_absent,
}


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


GOLDEN_CASES: list[dict[str, Any]] = _golden()["cases"]


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_baseline_artifacts_validate() -> None:
    """A well-formed corpus passes — otherwise the rejections prove nothing."""
    v.validate_manifest(manifest())
    v.validate_table("klines_4h", klines())
    v.validate_table(
        "pit_universe",
        pa.Table.from_pylist([pit_universe_row()], schema=PIT_UNIVERSE_SCHEMA),
    )
    v.validate_outcomes(outcomes(), klines(), _DECISIONS)
    v.validate_premium_index_gap(gap_audit(), pit_universe(), IN_WINDOW_GAP_SYMBOLS)
    v.validate_corpus(
        manifest(),
        pit_universe=pit_universe(),
        gap_audit=gap_audit(),
        klines=klines(),
        premium_index=premium_index(),
        outcomes=outcomes(),
        decisions=_DECISIONS,
    )


@pytest.mark.unit
def test_validate_corpus_covers_every_canonical_table() -> None:
    """A canonical table absent from the orchestrator is a schema nobody checks.

    ``validate_corpus`` is the whole entry point, so the set of tables it reaches
    has to equal ``CANONICAL_TABLES`` — otherwise adding a table to the contract
    silently adds one that no run validates.
    """
    reached: set[str] = set()
    original = v.validate_table

    def spy(name: str, table: pa.Table) -> None:
        reached.add(name)
        original(name, table)

    v.validate_table = spy  # type: ignore[assignment]
    try:
        v.validate_corpus(
            manifest(),
            pit_universe=pit_universe(),
            gap_audit=gap_audit(),
            klines=klines(),
            premium_index=premium_index(),
            outcomes=outcomes(),
            decisions=_DECISIONS,
        )
    finally:
        v.validate_table = original  # type: ignore[assignment]

    assert reached == set(CANONICAL_TABLES), {
        "unvalidated": sorted(set(CANONICAL_TABLES) - reached),
        "unexpected": sorted(reached - set(CANONICAL_TABLES)),
    }


@pytest.mark.unit
def test_not_eligible_gap_symbol_is_no_impact() -> None:
    """The other admissible baseline: no kline evidence, so nothing to rank."""
    v.validate_premium_index_gap(
        gap_audit([gap_audit_row(status=c.NOT_ELIGIBLE, quote_volume=None)]),
        pit_universe(),
        IN_WINDOW_GAP_SYMBOLS,
    )


@pytest.mark.unit
def test_input_evidence_outranks_every_other_terminal_code() -> None:
    """MUTANT ④: an artifact bad in two ways reports the input-evidence code.

    The outcome table here is *also* broken (a hand-written return), so a
    validator that adjudicated in declaration order would report
    ``RUN_INVALID_OUTCOME_EVIDENCE`` and the operator would go looking at
    outcome arithmetic for a corpus whose ranking input was never admissible.
    """
    bad_outcomes = [dict(r) for r in _OUTCOME_ROWS]
    bad_outcomes[0]["outcome_abs_log_return_bps"] = 250.0

    with pytest.raises(v.ContractViolation) as excinfo:
        v.validate_corpus(
            manifest(),
            pit_universe=pit_universe(),
            gap_audit=gap_audit([gap_audit_row(quote_volume="250.0")]),
            klines=klines(),
            premium_index=premium_index(),
            outcomes=outcomes(bad_outcomes),
            decisions=_DECISIONS,
        )
    assert excinfo.value.code == v.RUN_INVALID_INPUT_EVIDENCE

    # ...and the outcome defect really is there, so the assertion above is
    # about precedence and not about the second defect being absent.
    with pytest.raises(v.ContractViolation) as outcome_only:
        v.validate_outcomes(outcomes(bad_outcomes), klines(), _DECISIONS)
    assert outcome_only.value.code == v.RUN_INVALID_OUTCOME_EVIDENCE

    assert c.TERMINAL_CODE_PRIORITY[0] == v.RUN_INVALID_INPUT_EVIDENCE


@pytest.mark.unit
def test_gap_validator_offers_no_substitution_path() -> None:
    """MUTANT ①: there is nowhere for a replacement symbol to come out.

    The validator returns ``None`` and leaves its inputs alone, so a caller
    cannot obtain a "repaired" pool from it even by accident.
    """
    pool = pit_universe()
    before = pool.to_pylist()
    assert (
        v.validate_premium_index_gap(gap_audit(), pool, IN_WINDOW_GAP_SYMBOLS) is None
    )
    assert pool.to_pylist() == before

    tree = ast.parse(Path(v.__file__).read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    entry = functions["validate_premium_index_gap"]
    assert isinstance(entry.returns, ast.Constant) and entry.returns.value is None
    returned = [
        node
        for node in ast.walk(entry)
        if isinstance(node, ast.Return) and node.value is not None
    ]
    assert not returned, "the gap validator hands something back to its caller"

    # ``_ranked_pool`` computes the true ranking in order to *compare* it with
    # the declared one.  Handing that recomputed order back would be the
    # re-ranking itself, one call site away.
    ranker = functions["_ranked_pool"]
    handed_back = {
        node.value.id
        for node in ast.walk(ranker)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
    }
    assert handed_back == {"pool"}, handed_back


@pytest.mark.unit
def test_golden_registry_and_builders_agree() -> None:
    """Neither side may drift: no orphan case, no unexercised builder."""
    registry = {case["case_id"] for case in GOLDEN_CASES}
    assert registry == set(CASE_BUILDERS), {
        "in_registry_only": sorted(registry - set(CASE_BUILDERS)),
        "in_builders_only": sorted(set(CASE_BUILDERS) - registry),
    }
    assert len(GOLDEN_CASES) == len(registry), "duplicate case_id in golden registry"


@pytest.mark.unit
def test_golden_registry_pins_canonical_source() -> None:
    golden = _golden()
    assert golden["contract_id"] == c.CONTRACT_ID
    assert c.CANONICAL_SOURCE_SHA256.startswith(
        golden["canonical_source_sha256_prefix"]
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "case", GOLDEN_CASES, ids=[case["case_id"] for case in GOLDEN_CASES]
)
def test_bad_input_is_rejected(case: dict[str, Any]) -> None:
    builder = CASE_BUILDERS[case["case_id"]]
    with pytest.raises(v.ContractViolation) as excinfo:
        builder()
    violation = excinfo.value
    assert violation.code == case["expected_code"]
    assert violation.clause == case["clause"]
    assert case["expected_detail_contains"] in violation.detail, violation.detail


@pytest.mark.unit
def test_every_clause_has_at_least_one_golden_rejection() -> None:
    covered = {case["clause"] for case in GOLDEN_CASES}
    assert covered == set(c.CLAUSE_SOURCES), sorted(set(c.CLAUSE_SOURCES) - covered)
