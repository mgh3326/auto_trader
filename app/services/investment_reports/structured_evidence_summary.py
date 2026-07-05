"""ROB-715 — backend-derived summary of ``evidence_snapshot['structured_evidence']``.

The frontend renders the returned string verbatim (no client-side parsing of the
nested structure). Deterministic: keys are sorted so the output is stable across
requests and safe to snapshot-test.
"""

from __future__ import annotations

from typing import Any


def summarize_structured_evidence(evidence_snapshot: dict[str, Any]) -> str | None:
    se = (evidence_snapshot or {}).get("structured_evidence")
    if not isinstance(se, dict) or not se:
        return None
    keys = sorted(str(k) for k in se)
    return f"{len(keys)} evidence fields: " + ", ".join(keys)
