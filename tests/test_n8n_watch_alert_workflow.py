"""Regression tests for the `paperclip-watch-alert.json` n8n workflow.

Reason this file exists: when the reviewer caught ROB-178 PR #541 v1, the Validate
& Dedupe node wrote `sentMap` before Discord actually sent. A Discord 5xx combined
with auto_trader retry therefore silently deduped the second attempt and the alert
was lost. The fix defers the `sentMap` write into a new `Mark Sent` code node that
runs only on the Discord success branch.

These tests shell out to `node` (following the existing
`tests/test_n8n_tc_briefing_discord.py` pattern) to execute the embedded jsCode
against a fake `$getWorkflowStaticData` / `$input` harness.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOW_PATH = Path("n8n/workflows/paperclip-watch-alert.json")

NODE_REQUIRED = shutil.which("node") is not None
pytestmark = pytest.mark.skipif(
    not NODE_REQUIRED, reason="node runtime required to execute embedded jsCode"
)


def _node_for(workflow: dict, node_id: str) -> dict:
    return next(node for node in workflow["nodes"] if node["id"] == node_id)


def _run_js_pipeline(static_data: dict, payload: dict) -> dict:
    """Runs Validate & Dedupe then (conditionally) Mark Sent against `payload`.

    Returns a dict:
        {
            "validate_output": <json from Validate & Dedupe>,
            "static_data_after_validate": <sentMap snapshot before Mark Sent>,
            "mark_sent_ran": <bool>,
            "static_data_after_mark": <sentMap snapshot after Mark Sent — same as
                                        after_validate when mark_sent_ran=False>,
        }
    """
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    validate_code = _node_for(workflow, "wa-process")["parameters"]["jsCode"]
    mark_sent_code = _node_for(workflow, "wa-mark-sent")["parameters"]["jsCode"]

    runner = r"""
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

function makeStaticAccessor(scope) {
  return () => scope;
}

// Stage 1: Validate & Dedupe
const staticScope = input.staticData;
const getStatic = makeStaticAccessor(staticScope);
const body = input.payload;
const fn1 = new Function(
  '$input', '$getWorkflowStaticData',
  input.validateCode + '\n//# run; will return via `return [...]` inside the body'
);
// The embedded script uses top-level `return`. Wrap in IIFE via Function body.
const validateFn = new Function(
  '$input', '$getWorkflowStaticData',
  '"use strict";\n' + input.validateCode
);
const validateOutput = validateFn(
  { first: () => ({ json: body }) },
  getStatic,
);
const validateJson = validateOutput[0].json;
const staticAfterValidate = JSON.parse(JSON.stringify(staticScope));

let markSentRan = false;
let staticAfterMark = staticAfterValidate;
if (['crypto', 'kr', 'us'].includes(validateJson.route)) {
  markSentRan = true;
  const markSentFn = new Function(
    '$input', '$getWorkflowStaticData', '$',
    '"use strict";\n' + input.markSentCode
  );
  const accessor = (name) => {
    if (name === 'Validate & Dedupe') {
      return { item: { json: validateJson } };
    }
    throw new Error(`unexpected \${name}`);
  };
  markSentFn(
    { all: () => [{ json: validateJson }] },
    getStatic,
    accessor,
  );
  staticAfterMark = JSON.parse(JSON.stringify(staticScope));
}

process.stdout.write(JSON.stringify({
  validate_output: validateJson,
  static_data_after_validate: staticAfterValidate,
  mark_sent_ran: markSentRan,
  static_data_after_mark: staticAfterMark,
}));
"""

    completed = subprocess.run(
        ["node", "-e", runner],
        input=json.dumps(
            {
                "staticData": static_data,
                "payload": payload,
                "validateCode": validate_code,
                "markSentCode": mark_sent_code,
            }
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _base_payload(**overrides) -> dict:
    payload = {
        "correlation_id": "corr-123",
        "as_of": "2026-04-17T03:47:00Z",
        "market": "crypto",
        "triggered": [
            {
                "symbol": "BTC/KRW",
                "condition_type": "price_above",
                "threshold": 100000000,
                "current": 100500000,
            }
        ],
        "message": "BTC breached 100M KRW",
    }
    payload.update(overrides)
    return payload


def test_validate_dedupe_does_not_write_sentmap_on_sent_path() -> None:
    """Blocking regression: sentMap must stay untouched until Mark Sent runs.

    Otherwise a Discord 5xx causes auto_trader's retry to hit the deduped path
    and the alert is lost.
    """
    result = _run_js_pipeline({"sentMap": {}}, _base_payload())

    assert result["validate_output"]["route"] == "crypto"
    assert result["static_data_after_validate"] == {"sentMap": {}}, (
        "Validate & Dedupe must not write sentMap on the sent path"
    )
    # Fingerprints must be exposed for Mark Sent to consume.
    fingerprints = result["validate_output"]["fingerprints"]
    assert isinstance(fingerprints, list) and len(fingerprints) == 1
    assert fingerprints[0]["fp"] == "crypto:BTC/KRW:price_above:100000000"
    assert fingerprints[0]["isNew"] is True


def test_mark_sent_records_fingerprints_after_discord_success() -> None:
    """Mark Sent runs on the Discord success branch and writes sentMap."""
    result = _run_js_pipeline({"sentMap": {}}, _base_payload())
    assert result["mark_sent_ran"] is True
    sent_map = result["static_data_after_mark"]["sentMap"]
    assert "crypto:BTC/KRW:price_above:100000000" in sent_map
    entry = sent_map["crypto:BTC/KRW:price_above:100000000"]
    assert isinstance(entry["lastSentAt"], int) and entry["lastSentAt"] > 0


def test_discord_failure_retry_redelivers_instead_of_deduping() -> None:
    """Simulates the exact scenario the reviewer flagged:

    1. Request 1 runs Validate & Dedupe, Discord fails — Mark Sent never runs.
    2. auto_trader retries with the same payload.
    3. Request 2 must still route to `crypto` (sent) — not `deduped`.
    """
    static_data: dict = {"sentMap": {}}

    first = _run_js_pipeline(static_data, _base_payload())
    assert first["validate_output"]["route"] == "crypto"
    # Simulate Discord failure: we keep the sentMap from *before* Mark Sent.
    static_after_failure = first["static_data_after_validate"]
    assert static_after_failure == {"sentMap": {}}

    second = _run_js_pipeline(static_after_failure, _base_payload())
    assert second["validate_output"]["route"] == "crypto", (
        "retry after Discord failure must still be routed as sent, not deduped"
    )
    assert second["validate_output"]["sentCount"] == 1


def test_second_delivery_within_cooldown_dedupes() -> None:
    """After a successful delivery + Mark Sent, an identical payload dedupes."""
    first = _run_js_pipeline({"sentMap": {}}, _base_payload())
    assert first["mark_sent_ran"] is True

    static_after_success = first["static_data_after_mark"]
    second = _run_js_pipeline(static_after_success, _base_payload())

    assert second["validate_output"]["route"] == "deduped"
    assert second["validate_output"]["sentCount"] == 0
    assert second["validate_output"]["dedupedCount"] == 1
    assert second["mark_sent_ran"] is False


def test_invalid_payloads_return_400_without_touching_sentmap() -> None:
    result = _run_js_pipeline(
        {"sentMap": {}},
        _base_payload(market="forex"),
    )
    assert result["validate_output"]["route"] == "invalid"
    assert result["validate_output"]["responseStatusCode"] == 400
    assert result["static_data_after_validate"] == {"sentMap": {}}
    assert result["mark_sent_ran"] is False


def test_threshold_type_guard_rejects_non_scalar() -> None:
    result = _run_js_pipeline(
        {"sentMap": {}},
        _base_payload(
            triggered=[
                {
                    "symbol": "BTC/KRW",
                    "condition_type": "price_above",
                    "threshold": {"value": 1},
                }
            ]
        ),
    )
    assert result["validate_output"]["route"] == "invalid"
    assert result["validate_output"]["errorCode"] == "invalid_triggered_item"


def test_discord_success_outputs_route_through_mark_sent_node() -> None:
    """Structural guard: every Discord main output must funnel into Mark Sent.

    Catches a future refactor that accidentally re-wires a Discord node straight
    back to `Respond 200 (sent)` and reintroduces the bug.
    """
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    for discord_id in ("Discord — Crypto", "Discord — KR", "Discord — US"):
        main = workflow["connections"][discord_id]["main"][0]
        assert len(main) == 1
        assert main[0]["node"] == "Mark Sent", (
            f"{discord_id} main output must go through Mark Sent, not {main[0]['node']}"
        )
    mark_main = workflow["connections"]["Mark Sent"]["main"][0]
    assert len(mark_main) == 1
    assert mark_main[0]["node"] == "Respond 200 (sent)"
