# scripts/shadow_replay_probe.py
"""P1 frozen-context reproducibility probe for the A' shadow replay harness
(ROB-697, M1).

Calls `investment_report_get_hermes_context` twice for the same
`snapshot_bundle_uuid` and checks that the FROZEN sections (`stage_inputs`,
`cited_snapshots`, `policy_version`, `market`, `market_session`,
`coverage_summary`) are byte-identical across the two calls, i.e. safe to
treat as stable input for a later K-replay batch. `dimension_evidence` and
`dimension_reports` read LIVE tables and are reported separately as
"live_section_drift" rather than treated as a failure.

Read-only: no stage_run / artifact rows are persisted, no broker / order /
watch / order-intent mutation, no in-process LLM provider (ROB-501) — this
only calls the existing read-only Hermes-context impl.

Usage (operator, requires live DB + a real bundle_uuid):
    SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true \\
        uv run python -m scripts.shadow_replay_probe <bundle_uuid>
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.mcp_server.tooling.investment_hermes_handlers import (
    investment_report_get_hermes_context_impl as get_ctx,
)

_FROZEN_KEYS = (
    "stage_inputs",
    "cited_snapshots",
    "policy_version",
    "market",
    "market_session",
    "coverage_summary",
)

_LIVE_SECTION_KEYS = ("dimension_evidence", "dimension_reports")


def compare_frozen(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Diff two `get_hermes_context` payloads for the same bundle.

    Pure (dict in, dict out) — no I/O. Compares the FROZEN keys via
    `json.dumps(..., sort_keys=True)` equality and lists which of the
    LIVE-reading keys differ.

    Returns ``{"frozen_identical": bool, "live_section_drift": list[str]}``.
    """
    frozen_a = {k: a.get(k) for k in _FROZEN_KEYS}
    frozen_b = {k: b.get(k) for k in _FROZEN_KEYS}
    frozen_identical = json.dumps(frozen_a, sort_keys=True) == json.dumps(
        frozen_b, sort_keys=True
    )
    live_section_drift = [
        k
        for k in _LIVE_SECTION_KEYS
        if json.dumps(a.get(k), sort_keys=True) != json.dumps(b.get(k), sort_keys=True)
    ]
    return {
        "frozen_identical": frozen_identical,
        "live_section_drift": live_section_drift,
    }


async def probe(bundle_uuid: str) -> int:
    """Call `get_hermes_context` twice for `bundle_uuid` and diff the result.

    Prints the diff as JSON. Returns exit code 0 if the frozen sections are
    identical, 1 if they drifted, 2 if the first call itself failed
    (`success=False`, e.g. bundle not found or feature-flag disabled).
    """
    a = await get_ctx(bundle_uuid)
    if not a.get("success"):
        print(json.dumps({"error": a}, ensure_ascii=False))
        return 2

    b = await get_ctx(bundle_uuid)
    result = compare_frozen(a, b)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["frozen_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(probe(sys.argv[1])))
