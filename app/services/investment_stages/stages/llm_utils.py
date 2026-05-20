"""Shared helpers for LLM-backed investment stages."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.schemas.investment_stages import StageCitation
from app.services.investment_stages.stages.base import StageContext


def collect_prior_stage_inputs(
    context: StageContext,
    *,
    evidence_fields: Sequence[str],
    detail_fields: Sequence[str],
) -> tuple[list[str], dict[str, list[str]], list[StageCitation]]:
    """Collect prompt details, evidence lists, and citations from prior artifacts."""
    prior_details: list[str] = []
    evidence: dict[str, list[str]] = {field: [] for field in evidence_fields}
    citations: list[StageCitation] = []

    for stage_type, artifact in context.prior_artifacts.items():
        detail_lines = [
            f"=== Stage: {stage_type} (Verdict: {artifact.verdict}, Confidence: {artifact.confidence}) ===",
            f"Summary: {artifact.summary}",
        ]
        for field in detail_fields:
            detail_lines.append(
                f"{_field_label(field)}: {getattr(artifact, field, None)}"
            )
        prior_details.append("\n".join(detail_lines) + "\n")

        for field in evidence_fields:
            evidence[field].extend(getattr(artifact, field, None) or [])
        citations.extend(artifact.cited_snapshots or [])

    return prior_details, evidence, citations


def strip_markdown_fence(text: str) -> str:
    """Return JSON text without optional Markdown code fences."""
    clean = text.strip()
    if not clean.startswith("```"):
        return clean
    lines = clean.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _field_label(field: str) -> str:
    labels: dict[str, str] = {
        "key_points": "Key Points",
        "buy_evidence": "Buy Evidence",
        "sell_evidence": "Sell Evidence",
        "risk_evidence": "Risk Evidence",
    }
    return labels.get(field, field.replace("_", " ").title())


def ensure_json_mapping(data: Any) -> dict[str, Any]:
    """Validate that an LLM JSON parse produced an object."""
    if isinstance(data, dict):
        return data
    raise ValueError("LLM response JSON must be an object")
