"""ROB-329 — ``validated_run_card.v1`` → /invest/reports citation contract.

Connects ``research/nautilus_scalping`` ``validated_run_card.v1`` /
``validated_signal_gate.v1`` artifacts to ``/invest/reports`` as auditable
evidence. The goal is **not** strategy recommendation — it is to make it
traceable which research run and which validation evidence a report cited.

Design decisions (locked in ROB-329):

* **Non-finite sanitization (JSON-safe / null).** Run cards carry
  ``profit_factor: Infinity`` when a fold has no losing trades. Python's
  ``json.dumps`` emits a bare ``Infinity`` token, which is rejected by
  PostgreSQL ``jsonb`` *and* JavaScript ``JSON.parse`` (RFC 8259). Every
  non-finite float (``inf``/``-inf``/``nan``) is therefore replaced by
  ``None`` before it can reach the DB or an API consumer. ``null`` is the
  only representation that is safe across all three, and it avoids surfacing
  a misleading standalone "Infinity" number as if it were an edge.
* **Verdict-first framing.** The citation headline is ``verdict`` +
  ``framing`` + ``trade_count`` (``n``). The bootstrap CI / Monte-Carlo
  numbers live nested under ``validation`` — never as standalone top-level
  fields — so a reader cannot mistake e.g. "bootstrap CI lower 25 > 0" on
  ``n=2`` trades for a positive edge. ``is_pass_stamp`` is ``True`` only for
  a ``validated`` verdict.
* **Monte-Carlo three-state.** MC evidence is ``present`` (valid run),
  ``absent`` (no MC block), or ``errored`` (block present but the run failed,
  e.g. ``{"error": "insufficient_data"}``).
* **Tolerant reproducibility.** ``strategy_hash`` may be ``null`` and
  ``artifacts`` may be ``[]``; both are carried through verbatim.
* **Unknown-schema fallback.** A payload whose ``schema_version`` is not
  ``validated_run_card.v1`` yields ``recognized=False`` rather than raising.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.json_safe import sanitize_non_finite

RUN_CARD_SCHEMA = "validated_run_card.v1"
GATE_SCHEMA = "validated_signal_gate.v1"

#: Source label stamped on a report item's ``evidence_snapshot`` entry.
EVIDENCE_SOURCE = "validated_run_card"


class RunCardCitation(BaseModel):
    """Citation-ready, JSON-safe view over a ``validated_run_card.v1`` payload."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    recognized: bool
    candidate: str | None = None
    hypothesis: str | None = None
    symbols: list[str] = Field(default_factory=list)
    window: dict[str, Any] | None = None
    verdict: str | None = None
    verdict_reasons: list[str] = Field(default_factory=list)
    trade_count: int | None = None
    framing: str | None = None
    #: True only for a ``validated`` verdict — an insufficient_data /
    #: not_validated run card is explicitly *not* a pass/edge stamp.
    is_pass_stamp: bool = False
    net_after_cost: dict[str, Any] | None = None
    #: ``{"bootstrap": {...} | None, "monte_carlo": {"state": ..., ...}}``
    validation: dict[str, Any] = Field(default_factory=dict)
    reproducibility: dict[str, Any] = Field(default_factory=dict)
    data_sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _monte_carlo_view(raw: Any) -> dict[str, Any]:
    """Collapse the MC block into a three-state view (present/absent/errored)."""
    if not isinstance(raw, Mapping) or not raw:
        return {"state": "absent"}
    sanitized = sanitize_non_finite(dict(raw))
    if "error" in sanitized:
        return {"state": "errored", **sanitized}
    return {"state": "present", **sanitized}


def _bootstrap_view(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping) or not raw:
        return None
    return sanitize_non_finite(dict(raw))


def build_run_card_citation(payload: Mapping[str, Any]) -> RunCardCitation:
    """Build a :class:`RunCardCitation` from a run-card payload.

    Tolerant by contract: an unknown schema, a missing Monte-Carlo block, a
    null ``strategy_hash`` or empty ``artifacts`` all produce a valid citation
    rather than raising. Non-finite metrics are sanitized to ``null``.
    """
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str):
        schema_version = "unknown"

    if schema_version != RUN_CARD_SCHEMA:
        # Unknown-schema fallback — echo the version, flag unrecognized,
        # never crash. Downstream renders this as "unrecognized evidence".
        return RunCardCitation(schema_version=schema_version, recognized=False)

    gate = payload.get("gate_report")
    gate = gate if isinstance(gate, Mapping) else {}

    verdict = payload.get("verdict")
    verdict = verdict if isinstance(verdict, str) else None

    # trade_count headline: prefer the gate's explicit count, else the
    # net-after-cost fold's trade tally.
    trade_count = gate.get("trade_count")
    nac = payload.get("net_after_cost")
    nac = nac if isinstance(nac, Mapping) else {}
    if not isinstance(trade_count, int):
        tc = nac.get("trades")
        trade_count = tc if isinstance(tc, int) else None

    validation_raw = payload.get("validation")
    validation_raw = validation_raw if isinstance(validation_raw, Mapping) else {}

    framing = payload.get("framing")
    framing = framing if isinstance(framing, str) else None

    return RunCardCitation(
        schema_version=schema_version,
        recognized=True,
        candidate=(
            payload.get("candidate")
            if isinstance(payload.get("candidate"), str)
            else None
        ),
        hypothesis=(
            payload.get("hypothesis")
            if isinstance(payload.get("hypothesis"), str)
            else None
        ),
        symbols=_as_str_list(gate.get("symbols")),
        window=(
            dict(gate["window"]) if isinstance(gate.get("window"), Mapping) else None
        ),
        verdict=verdict,
        verdict_reasons=_as_str_list(payload.get("verdict_reasons")),
        trade_count=trade_count,
        framing=framing,
        is_pass_stamp=(verdict == "validated"),
        net_after_cost=(sanitize_non_finite(dict(nac)) if nac else None),
        validation={
            "bootstrap": _bootstrap_view(validation_raw.get("bootstrap")),
            "monte_carlo": _monte_carlo_view(validation_raw.get("monte_carlo")),
        },
        reproducibility=(
            sanitize_non_finite(dict(payload["reproducibility"]))
            if isinstance(payload.get("reproducibility"), Mapping)
            else {}
        ),
        data_sources=_as_str_list(payload.get("data_sources")),
        warnings=_as_str_list(payload.get("warnings")),
    )


def build_run_card_evidence(
    *, snapshot_uuid: str, citation: RunCardCitation
) -> dict[str, Any]:
    """Build the ``evidence_snapshot`` entry that a report item stores to cite
    a run-card snapshot (reuses ``InvestmentReportItem.evidence_snapshot``).

    Headline-first per decision #4: ``verdict`` / ``framing`` / ``trade_count``
    + ``is_pass_stamp`` lead; the bootstrap CI and Monte-Carlo numbers stay
    nested under ``validation`` so they cannot be read as a standalone edge.
    The full sanitized artifact lives on the cited snapshot (fetch by
    ``snapshot_uuid``); this entry is the audit pointer + headline.
    """
    return {
        "source": EVIDENCE_SOURCE,
        "snapshot_uuid": snapshot_uuid,
        "schema_version": citation.schema_version,
        "recognized": citation.recognized,
        "candidate": citation.candidate,
        "symbols": citation.symbols,
        "verdict": citation.verdict,
        "verdict_reasons": citation.verdict_reasons,
        "trade_count": citation.trade_count,
        "framing": citation.framing,
        "is_pass_stamp": citation.is_pass_stamp,
        "net_after_cost": citation.net_after_cost,
        "validation": citation.validation,
        "reproducibility": citation.reproducibility,
    }
