"""ROB-376 item 2 — HermesContextPayload intraday delta fields (additive)."""

from __future__ import annotations

import uuid

from app.schemas.hermes_composition import HermesContextPayload


def _minimal(**kw) -> HermesContextPayload:
    base = {
        "snapshot_bundle_uuid": uuid.uuid4(),
        "bundle_status": "ready",
        "market": "us",
        "policy_version": "intraday_action_report_v1",
    }
    base.update(kw)
    return HermesContextPayload(**base)


def test_intraday_fields_default_none() -> None:
    payload = _minimal()
    assert payload.baseline_report_uuid is None
    assert payload.intraday_delta_block is None
    # context_version unchanged (additive, no version bump)
    assert payload.context_version == "hermes-context.v1"


def test_intraday_fields_roundtrip() -> None:
    base = uuid.uuid4()
    block = {"success": True, "levels_delta": {"summary": {"target_hit": 1}}}
    payload = _minimal(baseline_report_uuid=base, intraday_delta_block=block)
    dumped = payload.model_dump(mode="json")
    assert dumped["baseline_report_uuid"] == str(base)
    assert dumped["intraday_delta_block"] == block
