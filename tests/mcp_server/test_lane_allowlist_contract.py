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
    "claude-mock": 10,
    "crypto": 67,
    "fable-workbench": 29,
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
    "fable-workbench": "7d90c03e1d95fd18ac608c82bacb0a7b67580449832584b267e65d1a10811167",
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
