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
    "claude-mock": 8,
    "crypto": 65,
    "fable-workbench": 27,
    "fill-handoff": 54,
    "kr": 64,
    "krb1-cycle": 33,
    "orch-live": 52,
    "orch-mock": 8,
    "shadow-crypto": 0,
    "us": 66,
    "watch-alert-relay": 52,
}
LANE_SHA256 = {
    "claude-mock": "750ae813180ad05916657c0f61756bee2a79b607b99e663d37751a7c5e5c7937",
    "crypto": "6d0e3dec375b4067a6321c9e16f831d2aa78f709e63ddf8a21caca30b28803a8",
    "fable-workbench": "78194ea4939daf2750695dfdc2aaabbbdd3793b33adeeda1d27daea1b8176309",
    "fill-handoff": "12bc7c94d923138195c926c25eb8fedf65bdc5512a85a96ca3b722aeb37f30d5",
    "kr": "0bbc5d8fa369f0c85c16d4bb8083611470db9d12c42d2c26e74fb445fce18258",
    "krb1-cycle": "ba965de24a388e88c1b3a377a3815f0eb2616e29b99b1eea9e1262a4c329cbfa",
    "orch-live": "c5ba1f1021059e33e33930e924146c0f0e426c7139e089592dc0a5988edbf18b",
    "orch-mock": "750ae813180ad05916657c0f61756bee2a79b607b99e663d37751a7c5e5c7937",
    "shadow-crypto": "9589ec25bc0f716c7651dddee3fb81536bf998575d2b92a30a34cdd5514fbb54",
    "us": "e4cea9f8bb17cdb085cdab9ed7767ee832cef2b31fab3024a42843eda2d07ebb",
    "watch-alert-relay": "c5ba1f1021059e33e33930e924146c0f0e426c7139e089592dc0a5988edbf18b",
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
        assert basis in {"", "prompt", "sentry", "both"}, (
            f"{lane}: unknown audit basis {basis!r}"
        )
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
