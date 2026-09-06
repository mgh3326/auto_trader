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
    "claude-mock": 9,
    "crypto": 66,
    "fable-workbench": 28,
    "fill-handoff": 55,
    "kr": 65,
    "krb1-cycle": 34,
    "orch-live": 53,
    "orch-mock": 9,
    "shadow-crypto": 1,
    "us": 67,
    "watch-alert-relay": 53,
}
LANE_SHA256 = {
    "claude-mock": "1a598a68113d04715e388b9a79f4f1ebf9e9ee1ceeddea7eefc33407b7c331ad",
    "crypto": "b1f472d537193ef472b7b43b9ca67e99e0327b63a7a48814936e8d6a69cb51ce",
    "fable-workbench": "87c93ec245d04457803e6879d9a73aed29b54b8d6d834946a93d8e670f49e5ab",
    "fill-handoff": "cfd62580350cfc23fc9fead7df7ce30a5c23d796703d45503f932a67d6ad2593",
    "kr": "f44e243da2fa52aa2976ab5862ffbdb8b106c7bb1574d5d9b5fba72f3658d076",
    "krb1-cycle": "962bbb9b70c9c35a71d15ea0635a84fe59504eedec0fdfe271891f215d2601c2",
    "orch-live": "2657ff67d9664b857426a14d1024ebafb9424e4633199678e89a005297f32fac",
    "orch-mock": "1a598a68113d04715e388b9a79f4f1ebf9e9ee1ceeddea7eefc33407b7c331ad",
    "shadow-crypto": "ca565c27d6d8bfb34386f1fa0bc3457afa194961c9a1797d1d1c94e59195500a",
    "us": "05b7d5d61f969472fd6a198ca64684aa92437c03c0b6145edb2e04263b973fa7",
    "watch-alert-relay": "2657ff67d9664b857426a14d1024ebafb9424e4633199678e89a005297f32fac",
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
