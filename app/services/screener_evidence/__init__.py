"""Deterministic screener candidate-evidence builder (ROB-304)."""

from app.services.screener_evidence.builder import build_candidate_evidence
from app.services.screener_evidence.models import CandidateEvidence

__all__ = ["CandidateEvidence", "build_candidate_evidence"]
