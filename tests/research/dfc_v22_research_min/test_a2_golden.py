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
    KLINES_4H_SCHEMA,
    OUTCOMES_SCHEMA,
    PIT_UNIVERSE_SCHEMA,
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

_DECISIONS: list[dict[str, Any]] = [
    {
        "signal_epoch_open_time": T0,
        "candidate_any": "arm_dfc_2c_4h",
        "winner_symbol": "XRPUSDT",
    },
    {
        "signal_epoch_open_time": T1,
        "candidate_any": "arm_dfc_2c_4h",
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
) -> dict[str, Any]:
    return {
        "signal_epoch_open_time": epoch,
        "signal_epoch_close_time": epoch + BAR - 1,
        "candidate_any": "arm_dfc_2c_4h",
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
    _outcome_row(T0, "XRPUSDT", "p-xrp-t0", "p-xrp-t0n", "1.0000", "1.0100"),
    _outcome_row(T1, "SOLUSDT", "p-sol-t1", "p-sol-t1n", "2.0000", "1.9800"),
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
        "sources": [_source(kind) for kind in c.REQUIRED_SOURCE_KINDS],
        "tables": [
            {
                "name": name,
                "path": f"canonical/{name}.parquet",
                "sha256": "d" * 64,
                "row_count": 4,
                "byte_size": 2048,
            }
            for name in ("klines_4h", "premium_index_4h", "pit_universe", "outcomes")
        ],
        "admissibility": {
            "basis": c.ADMISSIBILITY_BASIS,
            "independent_recollection_required": True,
            "comparison_keys": list(c.ADMISSIBILITY_COMPARISON_KEYS),
        },
    }
    base.update(overrides)
    return base


def pit_universe_row() -> dict[str, Any]:
    return {
        "epoch_open_time": T0,
        "rank": 1,
        "symbol": "XRPUSDT",
        "quote_volume_lookback": "123456.7",
        "lookback_start_time": T0 - 30 * 24 * 60 * 60 * 1000,
        "lookback_end_time": T0,
        "lifecycle_evidence_sha256": "e" * 64,
        "eligibility_evidence_sha256": "f" * 64,
        **_provenance("p-univ-t0"),
    }


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
    rows = [dict(r) for r in _OUTCOME_ROWS]
    rows[0]["candidate_any"] = "arm_relabelled_by_hand"
    v.validate_outcomes(outcomes(rows), klines(), _DECISIONS)


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
