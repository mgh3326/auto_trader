"""Tools removed from the MCP surface by an explicit operator decision.

hk 6930, operator-approved 2026-11-27: external report composition is retired.
Only these names may be missing from the audited 2026-09-03 classification, and
only the rows below may leave the audited lane manifests. Adding a name here
needs a new operator decision.
"""

from __future__ import annotations

_CREATE = "investment_report_create_from_hermes_composition"
_GET_CONTEXT = "investment_report_get_hermes_context"
_PREPARE_BUNDLE = "investment_report_prepare_bundle"
_PREPARE_INTRADAY = "investment_report_prepare_intraday_context"
_INGEST_ARTIFACTS = "investment_stage_artifacts_ingest_from_hermes"

OPERATOR_RETIRED_TOOL_NAMES = frozenset(
    {_CREATE, _GET_CONTEXT, _PREPARE_BUNDLE, _PREPARE_INTRADAY, _INGEST_ARTIFACTS}
)

# Lane manifest rows (all with basis "prompt") that the decision removed.
OPERATOR_APPROVED_LANE_REMOVALS: dict[str, tuple[str, ...]] = {
    "crypto": (_CREATE,),
    "fill-handoff": (_CREATE,),
    "kr": (_CREATE,),
    "orch-live": (_CREATE,),
    "us": (_CREATE, _GET_CONTEXT, _PREPARE_BUNDLE),
    "watch-alert-relay": (_CREATE,),
}
