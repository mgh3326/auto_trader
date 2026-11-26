"""Promoted lane contracts from the 2026-09-03 audit's Lane table.

A lane can connect to several profiles: its required tools must exist in their
UNION. The manifests retain the exact reviewed tool/basis bytes, including the
intentionally empty shadow-crypto lane. Hashes prevent vacuous contracts after
accidental row deletion or coordinated edits to both draft and promoted files.
Rows may only be removed by the operator-approved list in
``_operator_retired_tools``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.mcp_server._operator_retired_tools import (
    OPERATOR_APPROVED_LANE_REMOVALS,
)
from tests.mcp_server._registration_recorder import collect_profile_tools

pytestmark = pytest.mark.unit
REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_DIR = REPO_ROOT / "config" / "mcp_lane_allowlists"
DRAFT_DIR = REPO_ROOT / "lane-allowlists.draft"

# Exact Lane -> Profiles and Draft tools columns; no inferred server mapping.
LANE_PROFILES: dict[str, tuple[str, ...]] = {
    "claude-mock": ("hermes-paper-kis", "us-paper"),
    "crypto": ("crypto", "default"),
    "fill-handoff": ("crypto", "default"),
    "kr": ("default",),
    "orch-live": ("default",),
    "orch-mock": ("hermes-paper-kis", "us-paper"),
    "shadow-crypto": ("default",),
    "us": ("default",),
    "watch-alert-relay": ("default",),
}
LANE_COUNTS = {
    "claude-mock": 10,
    "crypto": 66,
    "fill-handoff": 55,
    "kr": 74,
    "orch-live": 54,
    "orch-mock": 10,
    "shadow-crypto": 1,
    "us": 69,
    "watch-alert-relay": 53,
}
LANE_SHA256 = {
    "claude-mock": "e942cf3f43f184fb6c5893e53582ad027d2e9abd50a19126d6e52c1fd905cd36",
    "crypto": "0e85ebf6f5f30859d7e9938f7d1d4f788a0a12346e54d34909cefb7ed723c4eb",
    "fill-handoff": "67540b76dabb9f4f6c957fcde0f8c437d1f189802f119ca0da5cc4c25057fad4",
    "kr": "c2b150df2f16a5e0cd861407575a802d1a04906d07c276ced9fb2d53518ca56e",
    "orch-live": "7ed64733f151151a62acdb2bb73ec2dbc9ee29416826b69383e4b87de9e6e268",
    "orch-mock": "e942cf3f43f184fb6c5893e53582ad027d2e9abd50a19126d6e52c1fd905cd36",
    "shadow-crypto": "ca565c27d6d8bfb34386f1fa0bc3457afa194961c9a1797d1d1c94e59195500a",
    "us": "e999db6563e2e5a7fb9e7bc2bf71d2c9bb67e968b80f410ceaac837b8f9c9734",
    "watch-alert-relay": "498771db46fccff866639e6991e57779418b51d36f11091595398ae0c02dd2eb",
}

# sha256 of each manifest's sorted rows as audited before hk 6930. A manifest
# must equal that row set minus the operator-approved removals, so any other row
# removal fails even when LANE_SHA256 / LANE_COUNTS are re-pinned. A reviewed
# row addition updates this digest too.
AUDITED_ROWS_SHA256 = {
    "claude-mock": "6f5072d656d6aa671f80268c91423fcfa5140194c8dbd8ecc56f8706eeb3fd83",
    "crypto": "8278134349ff050c3076ae318fa8de0c4001dac8557393f68fcec1daabf08478",
    "fill-handoff": "35b3aeddfd08342fb85be412a40974c316e7dd3a94cdf4e19d975252a9d1bc30",
    "kr": "4b9d9698abff9ac521ec4afa359a7592b1f9f50c1a9062674644d72afe67dda6",
    "orch-live": "9fa243f3443a135c9c9d3626328c6376a0f34ade1303032e73b78b45fbf2d2cf",
    "orch-mock": "6f5072d656d6aa671f80268c91423fcfa5140194c8dbd8ecc56f8706eeb3fd83",
    "shadow-crypto": "7c87e18cf88915f4e64bdcccc72af68625cee7c7a8054a1e51b467fc4fa81337",
    "us": "722eec171c91bf816dcb61b0746a0d53154df41932f76f6f58c4bd529c8eab87",
    "watch-alert-relay": "9bb160230a0b330024d4b1a41fbae8853d50b2e9319b6e15d35b7f231517d1e4",
}


def _read_allowlist(lane: str) -> set[str]:
    path = ALLOWLIST_DIR / f"{lane}.txt"
    assert path.is_file(), f"{lane}: promoted lane allowlist is missing"
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == LANE_SHA256[lane], (
        f"{lane}: audited tool/basis bytes changed"
    )
    draft = DRAFT_DIR / path.name
    assert draft.is_file(), f"{lane}: source draft is missing"
    assert content == draft.read_bytes(), f"{lane}: promotion changed draft bytes"
    rows = {
        line
        for line in content.decode("utf-8").splitlines()
        if line and not line.startswith("#")
    }
    removed = {
        f"{tool}\tprompt" for tool in OPERATOR_APPROVED_LANE_REMOVALS.get(lane, ())
    }
    assert not rows & removed, f"{lane}: operator-retired rows must stay removed"
    audited = "\n".join(sorted(rows | removed)).encode()
    assert hashlib.sha256(audited).hexdigest() == AUDITED_ROWS_SHA256[lane], (
        f"{lane}: row removal is not allowed (only the operator-approved "
        "hk 6930 rows may leave the audited manifest)"
    )
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
    assert (
        set(LANE_PROFILES)
        == set(LANE_COUNTS)
        == set(LANE_SHA256)
        == set(AUDITED_ROWS_SHA256)
    )
    assert set(OPERATOR_APPROVED_LANE_REMOVALS) <= set(LANE_PROFILES)
    assert {path.stem for path in ALLOWLIST_DIR.glob("*.txt")} == set(LANE_PROFILES), (
        "promoted allowlists must contain exactly the audit's retained lanes"
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


@pytest.mark.asyncio
async def test_get_order_history_paper_route_does_not_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_order_history must not lazily persist on a read.

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
    register_all_tools(cast(Any, recorder), profile=McpProfile.DEFAULT)
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
