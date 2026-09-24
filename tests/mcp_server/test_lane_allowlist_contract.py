"""Promoted lane contracts from the 2026-09-03 audit's Lane table.

A lane can connect to several profiles: its required tools must exist in their
UNION. The manifests retain the exact reviewed tool/basis bytes, including the
intentionally empty shadow-crypto lane. Hashes prevent vacuous contracts after
accidental row deletion or coordinated edits to both draft and promoted files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.mcp_server.tooling.route_request_lanes import MUTATION_TOOLS
from tests.mcp_server._registration_recorder import collect_profile_tools

pytestmark = pytest.mark.unit
REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_DIR = REPO_ROOT / "config" / "mcp_lane_allowlists"
DRAFT_DIR = REPO_ROOT / "lane-allowlists.draft"

# Exact Lane -> Profiles and Draft tools columns; no inferred server mapping.
LANE_PROFILES: dict[str, tuple[str, ...]] = {
    "claude-mock": ("hermes-paper-kis", "kiwoom", "us-paper"),
    "crypto": ("crypto", "default"),
    "fable-workbench": ("analysis_readonly",),
    "fill-handoff": ("crypto", "default"),
    "kr": ("default",),
    "krb1-cycle": ("kiwoom",),
    "orch-live": ("default",),
    "orch-mock": ("hermes-paper-kis", "kiwoom", "us-paper"),
    "shadow-crypto": ("default",),
    "us": ("default",),
    "watch-alert-relay": ("default",),
}
LANE_COUNTS = {
    "claude-mock": 10,
    "crypto": 67,
    "fable-workbench": 42,
    "fill-handoff": 56,
    "kr": 66,
    "krb1-cycle": 35,
    "orch-live": 54,
    "orch-mock": 10,
    "shadow-crypto": 1,
    "us": 68,
    "watch-alert-relay": 54,
}
LANE_SHA256 = {
    "claude-mock": "e942cf3f43f184fb6c5893e53582ad027d2e9abd50a19126d6e52c1fd905cd36",
    "crypto": "50adf2dd9f9660e18e3db3b361d1018ef81aa1e1cd4d65f07e8056d236c673ed",
    "fable-workbench": "198e3483250c7f72c98d90d9c32a7c7bd71a94edf8a35919350d276cc4dedcce",
    "fill-handoff": "bd1dbe0d34836f9b0e74890a10c27e21589b14f2dc3beea46b5dee9dfcffdc52",
    "kr": "094fead23286d6feeba1496bb7147b4d44d99245195feb984a266d6bfadc0837",
    "krb1-cycle": "6b5d6fdbc6076e1f88ddf6203893a10601b9698d3b98d3ac13fac960db4fc73c",
    "orch-live": "fea6739a48bc10e9707eff60d7aa1df987949f8ab0e041169bbeaf8a2202833e",
    "orch-mock": "e942cf3f43f184fb6c5893e53582ad027d2e9abd50a19126d6e52c1fd905cd36",
    "shadow-crypto": "ca565c27d6d8bfb34386f1fa0bc3457afa194961c9a1797d1d1c94e59195500a",
    "us": "9d0f81725169f8943a35609d23052bc365a4fe2539e833d61e32b3a05ca72a4a",
    "watch-alert-relay": "fea6739a48bc10e9707eff60d7aa1df987949f8ab0e041169bbeaf8a2202833e",
}


def _read_allowlist(lane: str) -> set[str]:
    path = ALLOWLIST_DIR / f"{lane}.txt"
    assert path.is_file(), f"{lane}: promoted lane allowlist is missing"
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == LANE_SHA256[lane], (
        f"{lane}: audited tool/basis bytes changed (row removal is not allowed)"
    )
    draft = DRAFT_DIR / path.name
    assert draft.is_file(), f"{lane}: source draft is missing"
    assert content == draft.read_bytes(), f"{lane}: promotion changed draft bytes"
    tools: set[str] = set()
    for line in content.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        assert len(columns) == 2, f"{lane}: expected tool<TAB>basis: {line!r}"
        tool, basis = columns
        assert tool and tool not in tools, f"{lane}: blank or duplicate tool {tool!r}"
        assert basis in {"", "prompt", "sentry", "both"} or (
            basis.startswith("spec:") and len(basis) > 5
        ), f"{lane}: unknown audit basis {basis!r}"
        tools.add(tool)
    assert len(tools) == LANE_COUNTS[lane], f"{lane}: audited lane rows disappeared"
    return tools


def test_all_audited_lane_manifests_are_present() -> None:
    assert set(LANE_PROFILES) == set(LANE_COUNTS) == set(LANE_SHA256)
    assert {path.stem for path in ALLOWLIST_DIR.glob("*.txt")} == set(LANE_PROFILES), (
        "promoted allowlists must contain exactly the audit's 11 lanes"
    )


@pytest.mark.parametrize("lane", sorted(LANE_PROFILES))
def test_lane_allowlist_is_registered(
    monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    required = _read_allowlist(lane)
    actual = collect_profile_tools(monkeypatch, gates_enabled=True)
    profiles = LANE_PROFILES[lane]
    assert set(profiles) <= set(actual), f"{lane}: assigned MCP profile is missing"
    registered = set().union(*(actual[profile] for profile in profiles))
    missing = sorted(required - registered)
    assert not missing, (
        f"{lane}: lane allowlist tools absent from assigned profiles {profiles}: {missing}"
    )


# HK #657 — the fable-workbench lane is read-only except for its three reviewed
# created_by-labeled persistence tools. The write denylist below is the union of
# the registrar's own FORBIDDEN set, the route_request mutation taxonomy, and
# the named DB writers that taxonomy predates (journal/retrospective/forecast
# writers). Adding a writer to ANALYSIS_READONLY_TOOL_NAMES — e.g. the
# forecast_resolve mutant — registers it and turns this test red.
_REVIEWED_LANE_PERSISTENCE_TOOLS = frozenset(
    {"analysis_artifact_save", "forecast_save", "session_context_append"}
)

# Read-only status helpers that the legacy route_request taxonomy keeps inside
# MUTATION_TOOLS; the lane legitimately exposes some of them.
_MUTATION_BUCKET_READS = frozenset(
    {
        "get_order_history",
        "kis_live_get_order_history",
        "kis_mock_get_order_history",
        "kiwoom_mock_get_order_history",
        "kiwoom_mock_get_order_detail",
        "kiwoom_mock_get_orderable_cash",
        "kiwoom_mock_get_positions",
        "toss_get_order_history",
        "toss_get_orderable_cash",
        "toss_get_positions",
    }
)

# Handler-level writers outside the mutation taxonomy: every one must stay out
# of both the lane manifest and the analysis_readonly registration.
_HANDLER_WRITE_TOOL_NAMES = frozenset(
    {
        "forecast_resolve",
        "forecast_save",
        "analysis_artifact_save",
        "session_context_append",
        "save_trade_journal",
        "update_trade_journal",
        "modify_journal_entry",
        "save_trade_retrospective",
        "save_position_intake_retrospective",
        "set_user_setting",
        "update_manual_holdings",
        "decision_table_apply",
        "investment_watch_void",
        "investment_watch_expire",
        "sweep_expired_watches",
    }
)


def test_fable_workbench_lane_has_no_write_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling.analysis_readonly_registration import (
        ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES,
        ANALYSIS_READONLY_TOOL_NAMES,
    )

    # The mutant tripwire: forecast_resolve must stay in the forbidden set so
    # that adding it to ANALYSIS_READONLY_TOOL_NAMES turns this test red — via
    # the disjoint check, the forbidden-leak check, or the snapshot test.
    assert "forecast_resolve" in ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES
    assert "forecast_resolve" not in ANALYSIS_READONLY_TOOL_NAMES
    assert ANALYSIS_READONLY_TOOL_NAMES.isdisjoint(
        ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES
    ), (
        "readonly allowlist overlaps forbidden names: "
        f"{sorted(ANALYSIS_READONLY_TOOL_NAMES & ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES)}"
    )

    denylist = (
        (
            ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES
            | MUTATION_TOOLS
            | _HANDLER_WRITE_TOOL_NAMES
        )
        - _REVIEWED_LANE_PERSISTENCE_TOOLS
        - _MUTATION_BUCKET_READS
    )

    lane_tools = _read_allowlist("fable-workbench")
    registered = set(
        collect_profile_tools(monkeypatch, gates_enabled=True)["analysis_readonly"]
    )
    lane_leaks = sorted(denylist & lane_tools)
    registered_leaks = sorted(denylist & registered)
    assert not lane_leaks, (
        f"fable-workbench lane exposes write/mutation tools: {lane_leaks}"
    )
    assert not registered_leaks, (
        f"analysis_readonly registers write/mutation tools: {registered_leaks}"
    )


@pytest.mark.asyncio
async def test_fable_get_order_history_paper_route_does_not_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lane-registered get_order_history must not lazily persist.

    The db_simulated/paper route previously reached
    PaperTradingService.create_account (session.add + commit) when no
    'default' paper account existed — a write on a read tool. This drives
    the actual registered handler with the real PaperTradingService over a
    recording session; any add/commit/flush/delete is a write leak.
    """
    from typing import Any, cast

    from app.mcp_server.profiles import McpProfile
    from app.mcp_server.tooling import paper_order_handler, register_all_tools
    from tests.mcp_server._registration_recorder import RegistrationRecorder

    recorder = RegistrationRecorder()
    register_all_tools(cast(Any, recorder), profile=McpProfile.ANALYSIS_READONLY)
    get_order_history = recorder.tools["get_order_history"]

    ops: list[str] = []

    class _Result:
        def scalar_one_or_none(self) -> None:
            return None

    class _RecordingSession:
        async def execute(self, stmt: Any) -> Any:
            ops.append("execute")
            return _Result()

        def add(self, obj: Any) -> None:
            ops.append("add")

        async def commit(self) -> None:
            ops.append("commit")

        async def flush(self) -> None:
            ops.append("flush")

        async def delete(self, obj: Any) -> None:
            ops.append("delete")

        async def refresh(self, obj: Any) -> None:
            ops.append("refresh")

        async def close(self) -> None:
            ops.append("close")

    class _SessionCM:
        async def __aenter__(self) -> _RecordingSession:
            return _RecordingSession()

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(paper_order_handler, "AsyncSessionLocal", lambda: _SessionCM())

    # Every selector spelling that resolves to the db_simulated route.
    for call_kwargs in (
        {"account_mode": "db_simulated"},
        {"account_mode": "paper"},
        {"account_mode": "simulated"},
        {"account_type": "paper"},
    ):
        result = await get_order_history(**call_kwargs)
        assert result["success"] is False, call_kwargs
        assert "not found" in result["error"], call_kwargs

    writes = sorted(set(ops) & {"add", "commit", "flush", "delete"})
    assert not writes, f"readonly get_order_history performed session writes: {ops}"
