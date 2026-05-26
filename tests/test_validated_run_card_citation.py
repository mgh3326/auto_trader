"""ROB-329 — validated_run_card.v1 → /invest/reports citation contract.

Pure (no DB) tests for the parser/sanitizer/citation builder. The vendored
fixture ``tests/fixtures/validated_run_card/run_card_insufficient_data.json``
is a faithful copy of the ROB-327 F1 smoke run card — it contains bare
``Infinity`` tokens (profit_factor with no losing trades), which Python's
``json.load`` reads as ``float('inf')``. The whole point of the sanitizer is
that the citation it produces is strict-JSON / Postgres-jsonb / JS-JSON.parse
safe, so we assert ``json.dumps(..., allow_nan=False)`` round-trips.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.schemas.validated_run_card import (
    RUN_CARD_SCHEMA,
    build_run_card_citation,
    build_run_card_evidence,
    sanitize_non_finite,
)

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "validated_run_card"
    / "run_card_insufficient_data.json"
)


def _load_fixture() -> dict:
    # NB: json.load is lenient and parses bare ``Infinity`` -> float('inf'),
    # which is exactly the raw shape ingestion receives.
    with _FIXTURE.open() as fh:
        return json.load(fh)


def _assert_strict_json_safe(obj) -> None:
    """Round-trip under strict JSON: rejects Infinity/-Infinity/NaN.

    ``allow_nan=False`` is the Postgres-jsonb / JavaScript JSON.parse contract.
    """
    json.dumps(obj, allow_nan=False)


# ---------------------------------------------------------------------------
# sanitize_non_finite
# ---------------------------------------------------------------------------
def test_sanitize_replaces_non_finite_with_null_recursively():
    raw = {
        "pf": float("inf"),
        "neg": float("-inf"),
        "nan": float("nan"),
        "finite": 1.5,
        "ints": [1, float("inf"), 3],
        "nested": {"a": float("nan"), "b": "ok", "c": 0.0},
        "text": "Infinity is a string here",
    }
    out = sanitize_non_finite(raw)
    assert out["pf"] is None
    assert out["neg"] is None
    assert out["nan"] is None
    assert out["finite"] == 1.5
    assert out["ints"] == [1, None, 3]
    assert out["nested"] == {"a": None, "b": "ok", "c": 0.0}
    assert out["text"] == "Infinity is a string here"
    _assert_strict_json_safe(out)


def test_sanitize_does_not_mutate_input():
    raw = {"pf": float("inf")}
    sanitize_non_finite(raw)
    assert math.isinf(raw["pf"])


# ---------------------------------------------------------------------------
# build_run_card_citation — happy path on the real smoke fixture
# ---------------------------------------------------------------------------
def test_citation_from_real_fixture_does_not_crash_and_is_json_safe():
    citation = build_run_card_citation(_load_fixture())
    dump = citation.model_dump()
    _assert_strict_json_safe(dump)


def test_citation_headline_carries_verdict_framing_and_trade_count():
    citation = build_run_card_citation(_load_fixture())
    assert citation.recognized is True
    assert citation.schema_version == RUN_CARD_SCHEMA
    assert citation.verdict == "insufficient_data"
    assert citation.candidate == "meanrev_zscore_fade"
    assert citation.symbols == ["XRPUSDT"]
    assert citation.trade_count == 2
    assert citation.framing is not None and "not a pass stamp" in citation.framing


def test_insufficient_data_is_not_a_pass_stamp():
    citation = build_run_card_citation(_load_fixture())
    assert citation.is_pass_stamp is False


def test_non_finite_profit_factor_sanitized_to_null():
    citation = build_run_card_citation(_load_fixture())
    assert citation.net_after_cost is not None
    assert citation.net_after_cost["profit_factor"] is None
    # finite siblings survive
    assert citation.net_after_cost["win_rate_pct"] == 100.0


def test_bootstrap_numbers_are_nested_under_validation_not_top_level():
    # decision #4 — the bootstrap CI must never read as a standalone edge.
    citation = build_run_card_citation(_load_fixture())
    dump = citation.model_dump()
    assert "ci_lower" not in dump
    assert "observed_sharpe" not in dump
    assert dump["validation"]["bootstrap"]["ci_lower"] == pytest.approx(
        25.5811, rel=1e-3
    )


# ---------------------------------------------------------------------------
# Monte-Carlo 3-state — valid / absent / present-but-errored
# ---------------------------------------------------------------------------
def test_monte_carlo_errored_state_from_fixture():
    # The real smoke fixture's MC block carries {"error": "insufficient_data"}.
    citation = build_run_card_citation(_load_fixture())
    mc = citation.validation["monte_carlo"]
    assert mc["state"] == "errored"
    assert mc["error"] == "insufficient_data"


def test_monte_carlo_absent_state():
    payload = _load_fixture()
    del payload["validation"]["monte_carlo"]
    citation = build_run_card_citation(payload)
    assert citation.validation["monte_carlo"]["state"] == "absent"


def test_monte_carlo_present_valid_state():
    payload = _load_fixture()
    payload["validation"]["monte_carlo"] = {
        "actual_sharpe": 1.2,
        "actual_max_dd": -0.05,
        "p_value_sharpe": 0.03,
        "p_value_maxdd": 0.10,
        "n_sim": 200,
        "seed": 327,
    }
    citation = build_run_card_citation(payload)
    mc = citation.validation["monte_carlo"]
    assert mc["state"] == "present"
    assert mc["p_value_sharpe"] == 0.03


# ---------------------------------------------------------------------------
# Reproducibility — null strategy_hash + empty artifacts allowed
# ---------------------------------------------------------------------------
def test_null_strategy_hash_and_empty_artifacts_are_allowed():
    citation = build_run_card_citation(_load_fixture())
    repro = citation.reproducibility
    assert repro["config_hash"].startswith("870ff843")
    assert repro["strategy_hash"] is None
    assert repro["artifacts"] == []


# ---------------------------------------------------------------------------
# Unknown-schema fallback — must not crash
# ---------------------------------------------------------------------------
def test_unknown_schema_returns_unrecognized_citation_without_crashing():
    payload = {"schema_version": "something_else.v9", "candidate": "x"}
    citation = build_run_card_citation(payload)
    assert citation.recognized is False
    assert citation.schema_version == "something_else.v9"
    assert citation.verdict is None
    assert citation.is_pass_stamp is False
    _assert_strict_json_safe(citation.model_dump())


def test_missing_schema_version_falls_back_to_unrecognized():
    citation = build_run_card_citation({"candidate": "x"})
    assert citation.recognized is False
    assert citation.schema_version == "unknown"


# ---------------------------------------------------------------------------
# build_run_card_evidence — the report-item evidence_snapshot entry
# ---------------------------------------------------------------------------
def test_evidence_entry_is_headline_first_and_json_safe():
    citation = build_run_card_citation(_load_fixture())
    snap_uuid = "11111111-1111-1111-1111-111111111111"
    evidence = build_run_card_evidence(snapshot_uuid=snap_uuid, citation=citation)

    assert evidence["source"] == "validated_run_card"
    assert evidence["snapshot_uuid"] == snap_uuid
    assert evidence["schema_version"] == RUN_CARD_SCHEMA
    assert evidence["verdict"] == "insufficient_data"
    assert evidence["trade_count"] == 2
    assert evidence["is_pass_stamp"] is False
    assert "not a pass stamp" in evidence["framing"]

    # decision #4 — bootstrap/MC live under validation, never as standalone
    # top-level edge numbers.
    assert "ci_lower" not in evidence
    assert "observed_sharpe" not in evidence
    assert evidence["validation"]["monte_carlo"]["state"] == "errored"
    _assert_strict_json_safe(evidence)
