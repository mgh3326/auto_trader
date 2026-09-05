"""Lane allowlist contract (MCP tool usage audit, 2026-09-03).

Each consumer lane names, in `config/mcp_lane_allowlists/<lane>.txt`, the exact
tools its prompts/runners call. That manifest is the set the MCP surface must
keep registered: a surface-cleanup commit that drops a tool a live lane still
names is a production breakage, not a cleanup.

The registered surface is measured the same way the audit measured it — the
real `register_all_tools` executed against an in-memory recorder with every
feature gate enabled (`scripts.mcp_tool_usage_audit.collect_registry`). No
server, client, broker, or DB is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.mcp_tool_usage_audit import LANE_SPECS, collect_registry

ALLOWLIST_DIR = Path(__file__).resolve().parents[2] / "config" / "mcp_lane_allowlists"

# Lane -> MCP profiles, transcribed from the audit's "Lane allowlist draft and
# derivation design" table. Kept as a literal so the contract is readable here,
# and cross-checked against LANE_SPECS below so the two cannot drift.
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

VALID_BASES = {"prompt", "sentry", "both"}


def _read_manifest(lane: str) -> list[tuple[str, str]]:
    path = ALLOWLIST_DIR / f"{lane}.txt"
    entries: list[tuple[str, str]] = []
    for raw in path.read_text().splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        tool, _, basis = raw.partition("\t")
        entries.append((tool.strip(), basis.strip()))
    return entries


@pytest.fixture(scope="module")
def profile_tools() -> dict[str, set[str]]:
    """profile name -> set of tool names the real registrar registers."""
    registry = collect_registry()
    by_profile: dict[str, set[str]] = {}
    for tool, entry in registry.items():
        for profile in entry["profiles"]:
            by_profile.setdefault(profile, set()).add(tool)
    return by_profile


def test_lane_profile_map_matches_audit_lane_specs() -> None:
    """The literal above must equal the audit's own lane->profile assignment."""
    from_specs = {spec.name: tuple(spec.profiles) for spec in LANE_SPECS}
    assert LANE_PROFILES == from_specs, (
        "lane->profile map drifted from scripts.mcp_tool_usage_audit.LANE_SPECS; "
        f"test literal={LANE_PROFILES!r} LANE_SPECS={from_specs!r}"
    )


def test_manifest_files_match_declared_lanes() -> None:
    on_disk = {p.stem for p in ALLOWLIST_DIR.glob("*.txt")}
    assert on_disk == set(LANE_PROFILES), (
        "config/mcp_lane_allowlists/*.txt does not match the declared lanes; "
        f"on_disk={sorted(on_disk)} declared={sorted(LANE_PROFILES)}"
    )


@pytest.mark.parametrize("lane", sorted(LANE_PROFILES))
def test_manifest_lines_are_well_formed(lane: str) -> None:
    for tool, basis in _read_manifest(lane):
        assert tool, f"{lane}: manifest line has an empty tool name"
        assert basis in VALID_BASES, (
            f"{lane}: tool {tool!r} has basis {basis!r}, "
            f"expected one of {sorted(VALID_BASES)}"
        )


@pytest.mark.parametrize("lane", sorted(LANE_PROFILES))
def test_lane_allowlist_is_registered_by_its_assigned_profiles(
    lane: str, profile_tools: dict[str, set[str]]
) -> None:
    """Every tool a lane names must still be registered by one of its profiles.

    Union, not intersection: a lane with several assigned profiles runs under
    one of them per session (``orch-mock`` spans hermes-paper-kis / kiwoom /
    us-paper), and the draft manifests were derived from exactly that union
    (``build_lane_allowlists``'s ``any(profile in ...)``). Reading it as an
    intersection would demand the Alpaca tools from a KIS-only profile.

    This is the guard that makes a dead-tool drop safe: removing a tool a live
    lane still calls fails here, naming the lane and the tool.
    """
    manifest = {tool for tool, _ in _read_manifest(lane)}
    profiles = LANE_PROFILES[lane]
    for profile in profiles:
        assert profile in profile_tools, (
            f"lane {lane!r} is assigned profile {profile!r}, "
            "which registers no tools at all"
        )
    reachable: set[str] = set()
    for profile in profiles:
        reachable |= profile_tools[profile]
    missing = sorted(manifest - reachable)
    assert not missing, (
        f"lane {lane!r} names {len(missing)} tool(s) that no assigned profile "
        f"{list(profiles)} registers any more: {missing}"
    )


@pytest.mark.parametrize("lane", sorted(LANE_PROFILES))
def test_single_profile_lane_allowlist_is_fully_registered(
    lane: str, profile_tools: dict[str, set[str]]
) -> None:
    """For a lane pinned to one profile, union and intersection coincide.

    Stated separately so the strict per-profile guarantee is asserted where it
    is actually meaningful (kr / us / orch-live / watch-alert-relay /
    shadow-crypto / krb1-cycle / fable-workbench) instead of being weakened for
    every lane by the multi-profile ones.
    """
    profiles = LANE_PROFILES[lane]
    if len(profiles) != 1:
        pytest.skip(f"lane {lane!r} spans {len(profiles)} profiles")
    (profile,) = profiles
    manifest = {tool for tool, _ in _read_manifest(lane)}
    missing = sorted(manifest - profile_tools[profile])
    assert not missing, (
        f"lane {lane!r} is pinned to profile {profile!r}, which no longer "
        f"registers {len(missing)} tool(s) it names: {missing}"
    )
