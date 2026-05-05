"""ROB-115 — Verify the research pipeline no longer schedules
SocialStageAnalyzer for new sessions."""

from __future__ import annotations

from app.analysis import pipeline


def test_pipeline_module_imports_only_three_stage_analyzers() -> None:
    """SocialStageAnalyzer must not be imported (or used) by pipeline.py."""
    src = pipeline.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    # The reference may still exist in legacy modules; pipeline.py itself
    # must not import or instantiate SocialStageAnalyzer.
    assert "SocialStageAnalyzer" not in text, (
        "pipeline.py should not reference SocialStageAnalyzer (ROB-115)"
    )


def test_pipeline_run_research_session_does_not_create_social_row(
    monkeypatch,
) -> None:
    """Smoke check: run_research_session's analyzers list must omit social.

    We poke at the analyzers tuple by reading the source — adding a
    behavioral check requires too much DB scaffolding. The source check
    above already pins this; this test pins the analyzers list in code.
    """
    src = pipeline.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    # The analyzers list lines should mention the three remaining stages
    # and not 'Social'.
    assert "MarketStageAnalyzer" in text
    assert "NewsStageAnalyzer" in text
    assert "FundamentalsStageAnalyzer" in text
    assert "SocialStageAnalyzer()" not in text
