"""ROB-715 — deterministic one-line summary of evidence_snapshot.structured_evidence."""

from app.services.investment_reports.structured_evidence_summary import (
    summarize_structured_evidence,
)


def test_none_when_no_structured_evidence() -> None:
    assert summarize_structured_evidence({}) is None
    assert summarize_structured_evidence({"structured_evidence": None}) is None
    assert summarize_structured_evidence({"structured_evidence": {}}) is None


def test_summarizes_top_level_keys() -> None:
    snap = {
        "structured_evidence": {
            "valuation": "cheap",
            "momentum": "up",
            "risk": "low",
        }
    }
    out = summarize_structured_evidence(snap)
    assert out is not None
    # Deterministic: sorted keys, count-prefixed.
    assert "3" in out
    assert "momentum" in out and "risk" in out and "valuation" in out


def test_stable_ordering() -> None:
    snap = {"structured_evidence": {"b": 1, "a": 2}}
    assert summarize_structured_evidence(snap) == summarize_structured_evidence(
        {"structured_evidence": {"a": 2, "b": 1}}
    )
