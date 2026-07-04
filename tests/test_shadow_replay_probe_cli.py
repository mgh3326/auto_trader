# tests/test_shadow_replay_probe_cli.py
"""Unit tests for the shadow-replay P1 reproducibility probe (ROB-697).

`compare_frozen` is the pure diff logic extracted from the probe script so
it can be exercised without a DB / MCP tool call. The async `probe()`
wrapper (real two-call round trip against `investment_report_get_hermes_
context_impl`) is intentionally NOT exercised here — it requires a live DB,
a real bundle, and SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true, which is
an operator step (see docs/runbooks/shadow-replay.md).
"""

from __future__ import annotations

import pytest

from scripts import shadow_replay_probe as probe


@pytest.mark.unit
def test_frozen_identical_true_when_only_live_section_ignored_key_differs():
    a = {
        "stage_inputs": [{"stage_type": "x"}],
        "cited_snapshots": [{"snapshot_uuid": "u1"}],
        "policy_version": "intraday_action_report_v1",
        "market": "kr",
        "market_session": "regular",
        "coverage_summary": {"buy": 1},
        "dimension_evidence": {"a": 1},
    }
    b = {
        **a,
        "dimension_evidence": {"a": 1},
        # Non-frozen key differs (e.g. a verbatim-timestamp-free but
        # otherwise irrelevant field); frozen keys are identical.
        "some_other_field": "differs-across-calls",
    }

    result = probe.compare_frozen(a, b)

    assert result == {"frozen_identical": True, "live_section_drift": []}


@pytest.mark.unit
def test_frozen_identical_false_when_stage_inputs_differ():
    a = {
        "stage_inputs": [{"stage_type": "x"}],
        "cited_snapshots": [{"snapshot_uuid": "u1"}],
        "policy_version": "intraday_action_report_v1",
        "market": "kr",
        "market_session": "regular",
        "coverage_summary": {"buy": 1},
    }
    b = {
        **a,
        "stage_inputs": [{"stage_type": "y"}],
    }

    result = probe.compare_frozen(a, b)

    assert result["frozen_identical"] is False


@pytest.mark.unit
def test_drift_lists_dimension_evidence_when_it_differs():
    a = {
        "stage_inputs": [{"stage_type": "x"}],
        "cited_snapshots": [{"snapshot_uuid": "u1"}],
        "policy_version": "intraday_action_report_v1",
        "market": "kr",
        "market_session": "regular",
        "coverage_summary": {"buy": 1},
        "dimension_evidence": {"a": 1},
        "dimension_reports": [{"r": 1}],
    }
    b = {
        **a,
        "dimension_evidence": {"a": 2},
    }

    result = probe.compare_frozen(a, b)

    assert result["frozen_identical"] is True
    assert result["live_section_drift"] == ["dimension_evidence"]
