"""Real FastMCP protocol and semantic proof for the B0X shadow profile."""

from __future__ import annotations

import ast
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.shadow_replay_registration import (
    DENIED_CAPABILITY_FRAGMENTS,
    SHADOW_REPLAY_TOOL_NAMES,
    assert_shadow_replay_surface,
    build_shadow_replay_server,
)
from app.services.shadow_replay.portability import (
    DETERMINISTIC_COMPARISON_DOMAINS,
)
from tests.mcp_server._registration_recorder import collect_profile_tools

pytestmark = pytest.mark.unit

EVENT_ID = "kickoff-b0x-nudge-kr-2026-09-10"
INPUT_REF = f"artifact://input/{EVENT_ID}/snapshot"
SHADOW_CONTEXT_REF = f"session_context://canonical/resident-shadow/{EVENT_ID}"
LIVE_CONTEXT_REF = f"session_context://canonical/legacy/{EVENT_ID}"
SHADOW_REPORT_REF = f"artifact://resident-shadow/{EVENT_ID}/report"
LIVE_REPORT_REF = f"artifact://legacy/{EVENT_ID}/report"
PLAYBOOK_PATH = "docs/runbooks/b0x-kr-cycle.md"
DOMAINS: dict[str, Any] = {
    "permission": "observation-replay",
    "account": "kis-mock",
    "target": "fixture-lane",
    "intent": "observe-and-replay",
    "quantity_or_price_band": "no-order-band",
    "guard_decisions": {"broker": "denied", "proposal": "denied"},
}


def _text(result: object) -> str:
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in (getattr(result, "content", None) or [])
    )


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _write_fixture(root: Path) -> dict[str, dict[str, object]]:
    playbook = root / PLAYBOOK_PATH
    playbook.parent.mkdir(parents=True)
    playbook.write_text("# Fixture playbook\nObserve only.\n", encoding="utf-8")
    playbook_sha = hashlib.sha256(playbook.read_bytes()).hexdigest()

    body = {
        "date": "2026-09-10",
        "disposition": "cycle_kickoff",
        "playbook": PLAYBOOK_PATH,
        "source": "b0x",
        "slot": "b0x-nudge-kr",
        "tick": None,
    }
    (root / "event.json").write_text(
        json.dumps(
            {
                "type": "lane.event",
                "owner_lane": "fixture-lane",
                "event_id": EVENT_ID,
                "text": json.dumps(body, separators=(",", ":"), sort_keys=True),
            }
        ),
        encoding="utf-8",
    )
    snapshot = {
        "kind": "identical_input_snapshot",
        "artifact_ref": INPUT_REF,
        "availability_proof_ref": f"artifact://input/{EVENT_ID}/availability",
        "available": True,
        "source_event_id": EVENT_ID,
        "inputs": {"fixture": 1},
        "orders": [],
        "actions": [],
    }
    (root / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    context = {
        "kind": "canonical_session_context",
        "artifact_ref": SHADOW_CONTEXT_REF,
        "owner": "resident-shadow-owner",
        "provenance_ref": f"artifact://resident-shadow/{EVENT_ID}/context-provenance",
        "source_event_id": EVENT_ID,
        "input_snapshot_ref": INPUT_REF,
        "context": {"session": "fixture-session", "market": "KR"},
        "orders": [],
        "actions": [],
    }
    (root / "context.json").write_text(json.dumps(context), encoding="utf-8")

    live_output = {
        "artifact_ref": LIVE_REPORT_REF,
        "owner": "legacy-owner",
        "provenance_ref": f"artifact://legacy/{EVENT_ID}/report-provenance",
        "session_context_ref": LIVE_CONTEXT_REF,
        "session_context_owner": "legacy-owner",
        "session_context_provenance_ref": (
            f"artifact://legacy/{EVENT_ID}/context-provenance"
        ),
    }
    report = {
        "source_event_id": EVENT_ID,
        "input_snapshot_ref": INPUT_REF,
        "session_context_ref": SHADOW_CONTEXT_REF,
        "playbook_ref": PLAYBOOK_PATH,
        "playbook_sha256": playbook_sha,
        "deterministic_values": deepcopy(DOMAINS),
        "orders": [],
        "actions": [],
    }
    shadow_provenance = {
        "artifact_ref": SHADOW_REPORT_REF,
        "owner": "resident-shadow-owner",
        "provenance_ref": (f"artifact://resident-shadow/{EVENT_ID}/report-provenance"),
        "source_event_id": EVENT_ID,
        "input_snapshot_ref": INPUT_REF,
        "input_sha256": _digest(snapshot),
        "session_context_ref": SHADOW_CONTEXT_REF,
        "playbook_sha256": playbook_sha,
        "code_sha": "a" * 40,
    }
    live_snapshot = {
        "kind": "b0x_live_replay_snapshot",
        **live_output,
        "source_event_id": EVENT_ID,
        "input_snapshot_ref": INPUT_REF,
        "domains": deepcopy(DOMAINS),
        "orders": [],
        "actions": [],
    }
    shadow_snapshot = {
        "kind": "b0x_shadow_replay_snapshot",
        "artifact_ref": SHADOW_REPORT_REF,
        "owner": "resident-shadow-owner",
        "provenance_ref": shadow_provenance["provenance_ref"],
        "session_context_ref": SHADOW_CONTEXT_REF,
        "session_context_owner": "resident-shadow-owner",
        "session_context_provenance_ref": context["provenance_ref"],
        "source_event_id": EVENT_ID,
        "input_snapshot_ref": INPUT_REF,
        "domains": deepcopy(DOMAINS),
        "orders": [],
        "actions": [],
    }
    comparison = {
        field: {"live": value, "shadow": value, "match": True}
        for field, value in DOMAINS.items()
    }
    raw_provenance = {
        "artifact_ref": f"artifact://resident-shadow/{EVENT_ID}/raw",
        "owner": "resident-shadow-owner",
        "provenance_ref": f"artifact://resident-shadow/{EVENT_ID}/raw-provenance",
        "source_event_id": EVENT_ID,
        "input_snapshot_ref": INPUT_REF,
        "live_artifact_ref": LIVE_REPORT_REF,
        "live_owner": "legacy-owner",
        "live_provenance_ref": live_output["provenance_ref"],
        "shadow_artifact_ref": SHADOW_REPORT_REF,
        "shadow_owner": "resident-shadow-owner",
        "shadow_provenance_ref": shadow_provenance["provenance_ref"],
        "comparison_sha256": _digest(comparison),
    }
    return {
        "canonical_session_context_read": {
            "artifact_root": str(root),
            "relative_path": "context.json",
        },
        "source_event_observe": {
            "artifact_root": str(root),
            "relative_path": "event.json",
        },
        "emitted_trigger_playbook_read": {
            "artifact_root": str(root),
            "relative_path": PLAYBOOK_PATH,
            "source_event_relative_path": "event.json",
        },
        "identical_input_snapshot_read": {
            "artifact_root": str(root),
            "relative_path": "snapshot.json",
        },
        "shadow_report_write": {
            "artifact_root": str(root),
            "relative_path": "shadow-report.json",
            "report": report,
            "provenance": shadow_provenance,
            "live_output_provenance": live_output,
            "source_event_relative_path": "event.json",
            "playbook_relative_path": PLAYBOOK_PATH,
            "input_snapshot_relative_path": "snapshot.json",
            "session_context_relative_path": "context.json",
        },
        "deterministic_replay_compare": {
            "live_snapshot": live_snapshot,
            "shadow_snapshot": shadow_snapshot,
        },
        "raw_difference_artifact_write": {
            "artifact_root": str(root),
            "relative_path": "raw-difference.json",
            "differences": {
                "observed_at": {
                    "live": "2026-09-10T09:05:00+09:00",
                    "shadow": "2026-09-10T09:05:01+09:00",
                }
            },
            "classifications": {"observed_at": "temporal"},
            "provenance": raw_provenance,
            "live_snapshot": live_snapshot,
            "shadow_snapshot": shadow_snapshot,
        },
    }


@pytest.mark.asyncio
async def test_real_tools_list_is_exact_closed_world() -> None:
    server = build_shadow_replay_server()
    assert await assert_shadow_replay_surface(server) == SHADOW_REPLAY_TOOL_NAMES
    async with Client(server) as client:
        offered = frozenset(tool.name for tool in await client.list_tools())
    assert offered == SHADOW_REPLAY_TOOL_NAMES
    assert all(
        fragment not in name
        for name in offered
        for fragment in DENIED_CAPABILITY_FRAGMENTS
    )


@pytest.mark.asyncio
async def test_all_seven_handlers_bind_pr49_semantics_over_real_mcp(
    tmp_path: Path,
) -> None:
    calls = _write_fixture(tmp_path)
    server = build_shadow_replay_server()
    bodies: dict[str, dict[str, object]] = {}
    async with Client(server) as client:
        for name in SHADOW_REPLAY_TOOL_NAMES:
            result = await client.call_tool_mcp(name, calls[name])
            assert result.isError is False, f"{name}: {_text(result)}"
            bodies[name] = json.loads(_text(result))

    assert set(bodies) == SHADOW_REPLAY_TOOL_NAMES
    assert all(body["dry_run"] is True for body in bodies.values())
    assert all(body["orders"] == [] for body in bodies.values())
    assert all(body["actions"] == [] for body in bodies.values())
    assert bodies["source_event_observe"]["consumed"] is False
    assert bodies["emitted_trigger_playbook_read"]["source_event_id"] == EVENT_ID
    assert bodies["identical_input_snapshot_read"]["snapshot"]["artifact_ref"] == (
        INPUT_REF
    )
    compared = bodies["deterministic_replay_compare"]
    assert compared["deterministic"] is True
    assert compared["unexplained_difference_count"] == 0
    assert set(compared["deterministic_comparison"]) == set(
        DETERMINISTIC_COMPARISON_DOMAINS
    )
    assert all(
        entry["match"] is True and entry["live"] == entry["shadow"]
        for entry in compared["deterministic_comparison"].values()
    )

    report = json.loads((tmp_path / "shadow-report.json").read_text(encoding="utf-8"))
    assert report["owner"] == "resident-shadow-owner"
    assert report["artifact_ref"] != report["live_output_provenance"]["artifact_ref"]
    assert report["provenance"]["source_event_id"] == EVENT_ID
    assert report["orders"] == [] and report["actions"] == []
    raw = json.loads((tmp_path / "raw-difference.json").read_text(encoding="utf-8"))
    assert raw["kind"] == "b0x_raw_replay_difference"
    assert (
        raw["differences"]["observed_at"]["live"]
        != (raw["differences"]["observed_at"]["shadow"])
    )
    assert raw["provenance"]["artifact_ref"] not in {
        raw["provenance"]["live_artifact_ref"],
        raw["provenance"]["shadow_artifact_ref"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    (
        "missing_domain",
        "extra_domain",
        "aliased_owner",
        "nonempty_action",
        "incomplete_raw_provenance",
        "raw_reclassifies_domain",
        "raw_unbound_live_owner",
        "raw_unbound_comparison",
        "raw_deterministic_drift",
        "unbound_input",
        "aliased_context_owner",
    ),
)
async def test_semantic_mutants_fail_through_real_mcp(
    tmp_path: Path, scenario: str
) -> None:
    calls = _write_fixture(tmp_path)
    if scenario in {
        "missing_domain",
        "extra_domain",
        "aliased_owner",
        "nonempty_action",
    }:
        name = "deterministic_replay_compare"
        arguments = deepcopy(calls[name])
        shadow = arguments["shadow_snapshot"]
        assert isinstance(shadow, dict)
        domains = shadow["domains"]
        assert isinstance(domains, dict)
        if scenario == "missing_domain":
            domains.pop("permission")
        elif scenario == "extra_domain":
            domains["decision"] = "observe"
        elif scenario == "aliased_owner":
            shadow["owner"] = "legacy-owner"
        else:
            shadow["actions"] = ["place"]
    elif scenario in {
        "incomplete_raw_provenance",
        "raw_reclassifies_domain",
        "raw_unbound_live_owner",
        "raw_unbound_comparison",
        "raw_deterministic_drift",
    }:
        name = "raw_difference_artifact_write"
        arguments = deepcopy(calls[name])
        provenance = arguments["provenance"]
        assert isinstance(provenance, dict)
        if scenario == "incomplete_raw_provenance":
            provenance.pop("comparison_sha256")
        elif scenario == "raw_reclassifies_domain":
            arguments["differences"] = {
                "permission": {"live": "read", "shadow": "read"}
            }
            arguments["classifications"] = {"permission": "temporal"}
        elif scenario == "raw_unbound_live_owner":
            provenance["live_owner"] = "another-live-owner"
        elif scenario == "raw_unbound_comparison":
            provenance["comparison_sha256"] = "b" * 64
        else:
            shadow = arguments["shadow_snapshot"]
            assert isinstance(shadow, dict)
            domains = shadow["domains"]
            assert isinstance(domains, dict)
            domains["permission"] = "mutation-capable"
    else:
        name = "shadow_report_write"
        arguments = deepcopy(calls[name])
        if scenario == "unbound_input":
            snapshot = json.loads((tmp_path / "snapshot.json").read_text())
            snapshot["source_event_id"] = "kickoff-b0x-nudge-kr-2026-09-11"
            (tmp_path / "snapshot.json").write_text(json.dumps(snapshot))
        else:
            context = json.loads((tmp_path / "context.json").read_text())
            context["owner"] = "legacy-owner"
            (tmp_path / "context.json").write_text(json.dumps(context))

    server = build_shadow_replay_server()
    async with Client(server) as client:
        result = await client.call_tool_mcp(name, arguments)
    assert result.isError is True
    assert any(
        marker in _text(result)
        for marker in (
            "field set differs",
            "must be distinct",
            "empty list",
            "must not reclassify",
            "identities differ",
            "not bound to replay snapshots",
            "zero unexplained deterministic differences",
        )
    ), _text(result)


@pytest.mark.asyncio
async def test_deterministic_domain_drift_is_counted_as_unexplained_over_real_mcp(
    tmp_path: Path,
) -> None:
    calls = _write_fixture(tmp_path)
    arguments = deepcopy(calls["deterministic_replay_compare"])
    shadow = arguments["shadow_snapshot"]
    assert isinstance(shadow, dict)
    domains = shadow["domains"]
    assert isinstance(domains, dict)
    domains["permission"] = "mutation-capable"

    server = build_shadow_replay_server()
    async with Client(server) as client:
        result = await client.call_tool_mcp("deterministic_replay_compare", arguments)
    assert result.isError is False, _text(result)
    body = json.loads(_text(result))
    assert body["deterministic"] is False
    assert body["unexplained_difference_count"] == 1
    assert body["deterministic_comparison"]["permission"] == {
        "live": "observation-replay",
        "shadow": "mutation-capable",
        "match": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denied",
    [
        "proposal_create",
        "place_order",
        "cancel_order",
        "watch_create",
        "broker_read",
        "order_execution",
        "automatic_approval",
        "action_apply",
    ],
)
async def test_denied_capability_names_are_physically_absent(denied: str) -> None:
    server = build_shadow_replay_server()
    async with Client(server) as client:
        result = await client.call_tool_mcp(denied, {})
    assert result.isError is True
    assert "Unknown tool" in _text(result)


def test_handler_dependency_boundary_and_lower_privilege(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    handler_paths = (
        root / "app/services/shadow_replay/portability.py",
        root / "app/mcp_server/tooling/shadow_replay_registration.py",
    )
    imports: set[str] = set()
    for path in handler_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
    denied_dependencies = ("broker", "orders", "proposal", "watch", "approval")
    assert not [
        dependency
        for dependency in imports
        if any(fragment in dependency.lower() for fragment in denied_dependencies)
    ]

    inventory = collect_profile_tools(monkeypatch, gates_enabled=True)
    shadow = set(inventory[McpProfile.SHADOW_REPLAY.value])
    default = set(inventory[McpProfile.DEFAULT.value])
    assert shadow == SHADOW_REPLAY_TOOL_NAMES
    assert len(shadow) < len(default)
    assert "place_order" in default
    assert "place_order" not in shadow


@pytest.mark.asyncio
async def test_artifact_root_escape_and_create_overwrite_fail_closed(
    tmp_path: Path,
) -> None:
    calls = _write_fixture(tmp_path)
    server = build_shadow_replay_server()
    async with Client(server) as client:
        escaped = await client.call_tool_mcp(
            "canonical_session_context_read",
            {"artifact_root": str(tmp_path), "relative_path": "../outside.json"},
        )
        first = await client.call_tool_mcp(
            "shadow_report_write", calls["shadow_report_write"]
        )
        overwrite = await client.call_tool_mcp(
            "shadow_report_write", calls["shadow_report_write"]
        )
    assert escaped.isError is True
    assert first.isError is False
    assert overwrite.isError is True
