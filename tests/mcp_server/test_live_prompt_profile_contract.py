"""Task #1003: live-session prompts vs the live-* MCP profiles they run on.

Incident 2026-09-29 22:35: us-2235 ran on the dedicated at-mcp-live-us unit,
which serves exactly the ``live-us`` profile of ``config/mcp_profiles/live.yaml``
(29 tools). The prompt it executes requires tools that profile does not
serve, and the session ended with 0 orders and 0 proposals.

Contract: every tool a live prompt REQUIRES as a step must be served by its
lane's live profile. The prompts live in auto_trader-operator, so the
required set is pinned in ``tests/fixtures/live_prompt_tool_requirements.yaml``
(operator commit + file:line evidence), and:

* the always-run tests compare that pin to live.yaml — no checkout needed;
* with ``AUTO_TRADER_OPERATOR_ROOT`` set, the drift tests re-read the prompts
  and fail when a referenced non-profile tool is unclassified in the pin or a
  pinned ref no longer holds;
* with ``ROBIN_PREFECT_AUTOMATIONS_ROOT`` set, the rep -> profile -> prompt
  lane mapping and the harness allowlist are checked against prefect.

The gap is open until the operator decides (hk:doc
incident/2026-09-29/live-profile-gap, options A/B/C). ``KNOWN_REQUIRED_GAP``
records it: the contract test is ``xfail(strict=True)`` for each lane with a
non-empty known gap, and ``test_known_gap_is_exact`` fails on any change to
the gap in either direction.

Flipping to strict after the decision: make the change (B: add tools to
live.yaml; C: shrink the prompts, re-pin the fixture at the new operator
commit), then empty that lane's ``KNOWN_REQUIRED_GAP`` entry. The xfail mark
disappears with it, and the strict xfail would XPASS-fail if the entry were
left stale. Option A (shared mode) changes nothing here; the tests stay as
they are and keep the gap visible.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.live_profile_registration import (
    LIVE_PROFILES,
    load_live_manifest,
)
from app.mcp_server.tooling.route_request_lanes import ALL_KNOWN_TOOLS
from tests.mcp_server._registration_recorder import collect_profile_tools

pytestmark = pytest.mark.unit

REQUIREMENTS_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "live_prompt_tool_requirements.yaml"
)
CLASSES = ("required", "conditional", "guarded", "not_required")
NOT_REQUIRED_KINDS = frozenset({"NEG", "DESC", "NA"})
LANES = ("kr", "us", "crypto")

# The open #1003 gap: required-by-prompt tools the lane's live profile does
# not serve, as of live.yaml at f2b6be139 and operator 6595658. Empty a lane's
# entry once the operator decision closes it (see module docstring).
KNOWN_REQUIRED_GAP: dict[str, frozenset[str]] = {
    "kr": frozenset(
        {
            "analysis_artifact_get",
            "get_holdings_news",
            "get_news",
            "get_trading_policy",
            "screen_stocks",
            "session_bootstrap_pack",
        }
    ),
    "us": frozenset(
        {
            "get_earnings_calendar",
            "get_holdings_news",
            "get_news",
            "get_top_stocks",
            "get_trading_policy",
            "screen_stocks_snapshot",
            "toss_get_order_history",
        }
    ),
    "crypto": frozenset(
        {
            "analyze_stock_batch",
            "get_holdings_news",
            "get_news",
            "get_trading_policy",
            "order_proposal_void",
        }
    ),
}

# Bootstrap tools every live prompt names (root CLAUDE.md §0). Their absence
# from an extraction means the scan read the wrong files or nothing at all.
_BOOTSTRAP_TOOLS = frozenset(
    {"get_operating_briefing", "route_request", "order_proposal_create"}
)
_MIN_EXTRACTED_PER_LANE = 30
_REF_RE = re.compile(r"^(?P<path>[A-Za-z0-9_./-]+):(?P<line>[1-9][0-9]*)$")
_PROMPT_FILE_RE = re.compile(r"prompts/[a-z0-9-]+\.md")
_PREFECT_TOOL_PREFIX = "mcp__auto_trader_local__"


def _requirements() -> dict[str, Any]:
    data = yaml.safe_load(REQUIREMENTS_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _lane(lane: str) -> dict[str, Any]:
    return _requirements()["lanes"][lane]


def _classified(lane: str, cls: str) -> dict[str, Any]:
    return _lane(lane)[cls] or {}


def _served(lane: str) -> set[str]:
    return set(_lane(lane)["served"])


def _pinned(lane: str) -> set[str]:
    """Every tool name the pin records for a lane (served + all classes)."""
    return _served(lane).union(*(_classified(lane, cls) for cls in CLASSES))


def _profile_tools(lane: str) -> frozenset[str]:
    profile = McpProfile(_lane(lane)["profile"])
    return load_live_manifest().spec_for(profile).selected_tool_names()


def _required_gap(lane: str) -> set[str]:
    return set(_classified(lane, "required")) - _profile_tools(lane)


# ---------------------------------------------------------------------------
# Always-run: pinned requirements vs live.yaml
# ---------------------------------------------------------------------------


class TestPinnedRequirementsShape:
    def test_lanes_and_profiles(self) -> None:
        data = _requirements()
        assert data["version"] == 1
        assert tuple(data["lanes"]) == LANES
        profiles = {data["lanes"][lane]["profile"] for lane in LANES}
        assert profiles == {profile.value for profile in LIVE_PROFILES}
        for lane in LANES:
            assert _lane(lane)["profile"] == f"live-{lane}"
            assert _lane(lane)["reps"], lane
            assert _lane(lane)["prompt_files"], lane

    def test_classes_are_disjoint_and_complete(self) -> None:
        for lane in LANES:
            served = _lane(lane)["served"]
            assert served and len(served) == len(set(served)), lane
            seen: dict[str, str] = dict.fromkeys(served, "served")
            for cls in CLASSES:
                assert cls in _lane(lane), f"{lane}: missing class {cls}"
                for tool, entry in _classified(lane, cls).items():
                    assert tool not in seen, (
                        f"{lane}: {tool} classified as both {seen[tool]} and {cls}"
                    )
                    seen[tool] = cls
                    assert entry["why"].strip(), f"{lane}/{tool}: empty why"
                    assert entry["refs"], f"{lane}/{tool}: no refs"
                    for ref in entry["refs"]:
                        assert _REF_RE.match(ref), f"{lane}/{tool}: bad ref {ref}"
                    if cls == "not_required":
                        assert entry["kind"] in NOT_REQUIRED_KINDS, (lane, tool)

    def test_every_classified_name_is_a_registered_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        universe = set(ALL_KNOWN_TOOLS).union(
            *collect_profile_tools(monkeypatch, gates_enabled=True).values()
        )
        unknown = {
            f"{lane}/{tool}"
            for lane in LANES
            for tool in _pinned(lane)
            if tool not in universe
        }
        assert not unknown, f"pinned names no registrar produces: {sorted(unknown)}"

    def test_only_required_entries_may_be_in_the_profile(self) -> None:
        # Non-required classes exist to account for non-profile references;
        # a conditional/guarded/not_required entry that the profile serves is
        # stale and would hide a reclassification.
        for lane in LANES:
            profile = _profile_tools(lane)
            stale = {
                f"{cls}/{tool}"
                for cls in CLASSES[1:]
                for tool in _classified(lane, cls)
                if tool in profile
            }
            assert not stale, f"{lane}: served by the profile, drop from pin: {stale}"


@pytest.mark.parametrize("lane", LANES)
def test_served_references_stay_in_profile(lane: str) -> None:
    """A live.yaml edit may not drop a tool the lane prompts reference."""
    dropped = _served(lane) - _profile_tools(lane)
    assert not dropped, (
        f"live-{lane} no longer serves prompt-referenced tools {sorted(dropped)}; "
        f"either restore them or reclassify them in {REQUIREMENTS_PATH.name} "
        "with the operator decision"
    )


@pytest.mark.parametrize("lane", LANES)
def test_known_gap_is_exact(lane: str) -> None:
    """The open gap is exactly KNOWN_REQUIRED_GAP — no silent growth or fix."""
    actual = _required_gap(lane)
    known = set(KNOWN_REQUIRED_GAP[lane])
    assert actual == known, (
        f"{lane}: required-by-prompt tools missing from live-{lane} changed. "
        f"newly missing={sorted(actual - known)} (a live.yaml edit dropped a "
        f"prompt-required tool, or the pin gained one); "
        f"now served={sorted(known - actual)} (update KNOWN_REQUIRED_GAP)"
    )


@pytest.mark.parametrize(
    "lane",
    [
        pytest.param(
            lane,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    f"#1003 open gap pending operator decision; live-{lane} "
                    f"lacks {sorted(KNOWN_REQUIRED_GAP[lane])}"
                ),
            ),
        )
        if KNOWN_REQUIRED_GAP[lane]
        else lane
        for lane in LANES
    ],
)
def test_live_prompt_required_tools_are_in_live_profile(lane: str) -> None:
    gap = _required_gap(lane)
    assert not gap, (
        f"live prompts for lane {lane} require tools live-{lane} does not "
        f"serve: {sorted(gap)}"
    )


# ---------------------------------------------------------------------------
# Drift: re-read the operator prompts (AUTO_TRADER_OPERATOR_ROOT)
# ---------------------------------------------------------------------------


def _operator_root() -> Path:
    configured = os.environ.get("AUTO_TRADER_OPERATOR_ROOT")
    if not configured:
        pytest.skip(
            "AUTO_TRADER_OPERATOR_ROOT is not set; prompt drift check needs an "
            "auto_trader-operator checkout (the pinned contract above still ran)"
        )
    root = Path(configured)
    if not (root / "live" / "CLAUDE.md").is_file():
        pytest.fail(
            f"AUTO_TRADER_OPERATOR_ROOT={root} lacks live/CLAUDE.md; point it at "
            "a current auto_trader-operator checkout"
        )
    return root


def _lane_files(lane: str) -> list[str]:
    return [*_requirements()["common_files"], *_lane(lane)["prompt_files"]]


def _token_re(names: set[str]) -> re.Pattern[str]:
    """Identifier-boundary match of bare or MCP-qualified tool names."""
    alternation = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(
        rf"(?:(?<={_PREFECT_TOOL_PREFIX})|(?<![A-Za-z0-9_]))"
        rf"({alternation})(?![A-Za-z0-9_])"
    )


def _extract(root: Path, lane: str, universe: set[str]) -> dict[str, list[str]]:
    pattern = _token_re(universe)
    found: dict[str, list[str]] = {}
    for rel in _lane_files(lane):
        path = root / rel
        if not path.is_file():
            pytest.fail(f"{lane}: pinned prompt file missing in checkout: {rel}")
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for name in set(pattern.findall(line)):
                found.setdefault(name, []).append(f"{rel}:{number}")
    return found


@pytest.fixture
def tool_universe(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    universe = set(ALL_KNOWN_TOOLS).union(
        *collect_profile_tools(monkeypatch, gates_enabled=True).values()
    )
    assert len(universe) >= 150, f"tool universe too small: {len(universe)}"
    return universe


def test_token_pattern_matches_qualified_and_bare_names() -> None:
    pattern = _token_re({"get_news", "get_news_extra", "screen_stocks"})
    line = (
        "call `get_news(symbol)`, mcp__auto_trader_local__screen_stocks, "
        "get_news_extra; not xget_news, kis_get_news or get_newsy"
    )
    assert pattern.findall(line) == ["get_news", "screen_stocks", "get_news_extra"]


@pytest.mark.parametrize("lane", LANES)
def test_prompt_references_are_all_classified(
    lane: str, tool_universe: set[str]
) -> None:
    root = _operator_root()
    extracted = _extract(root, lane, tool_universe)
    assert len(extracted) >= _MIN_EXTRACTED_PER_LANE, (
        f"{lane}: only {len(extracted)} tools extracted; refusing to pass vacuously"
    )
    assert _BOOTSTRAP_TOOLS <= set(extracted), (
        f"{lane}: bootstrap tools not found: {sorted(_BOOTSTRAP_TOOLS - set(extracted))}"
    )
    unpinned = {
        tool: refs for tool, refs in extracted.items() if tool not in _pinned(lane)
    }
    assert not unpinned, (
        f"{lane}: live prompts reference tools the pin does not record — add "
        f"each to served (if live-{lane} serves it) or classify it in "
        f"{REQUIREMENTS_PATH.name}: "
        f"{ {tool: refs[:4] for tool, refs in sorted(unpinned.items())} }"
    )


@pytest.mark.parametrize("lane", LANES)
def test_pinned_classifications_are_still_referenced(
    lane: str, tool_universe: set[str]
) -> None:
    root = _operator_root()
    extracted = _extract(root, lane, tool_universe)
    stale = sorted(_pinned(lane) - set(extracted))
    assert not stale, (
        f"{lane}: pinned tools no longer referenced by the lane prompts "
        f"(re-pin after the prompt change): {stale}"
    )


@pytest.mark.parametrize("lane", LANES)
def test_pinned_refs_still_hold(lane: str) -> None:
    root = _operator_root()
    drifted: list[str] = []
    for cls in CLASSES:
        for tool, entry in _classified(lane, cls).items():
            pattern = _token_re({tool})
            for ref in entry["refs"]:
                match = _REF_RE.match(ref)
                assert match, ref
                path = root / match["path"]
                lines = (
                    path.read_text(encoding="utf-8").splitlines()
                    if path.is_file()
                    else []
                )
                index = int(match["line"]) - 1
                if index >= len(lines) or not pattern.search(lines[index]):
                    drifted.append(f"{tool} @ {ref}")
    assert not drifted, (
        f"{lane}: pinned refs no longer hold at this operator checkout (pinned "
        f"operator_commit={_requirements()['source']['operator_commit']}); "
        f"re-pin the refs: {drifted}"
    )


# ---------------------------------------------------------------------------
# Lane mapping + harness allowlist (ROBIN_PREFECT_AUTOMATIONS_ROOT)
# ---------------------------------------------------------------------------


def _prefect_sessions_module() -> ast.Module:
    configured = os.environ.get("ROBIN_PREFECT_AUTOMATIONS_ROOT")
    if not configured:
        pytest.skip(
            "ROBIN_PREFECT_AUTOMATIONS_ROOT is not set; rep->profile mapping "
            "check needs a robin-prefect-automations checkout"
        )
    path = Path(configured) / "src" / "robin_automation" / "kr_live_sessions.py"
    if not path.is_file():
        pytest.fail(f"ROBIN_PREFECT_AUTOMATIONS_ROOT lacks {path}")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_literal(module: ast.Module, name: str) -> Any:
    for node in module.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if isinstance(target, ast.Name) and target.id == name and node.value:
            return ast.literal_eval(node.value)
    pytest.fail(f"kr_live_sessions.py has no literal {name}")


def test_rep_lane_mapping_matches_prefect() -> None:
    module = _prefect_sessions_module()
    reps: dict[str, dict[str, str]] = _module_literal(module, "REPS")
    rep_profiles: dict[str, str] = _module_literal(module, "REP_MCP_PROFILES")
    lane_by_profile = {_lane(lane)["profile"]: lane for lane in LANES}

    for lane in LANES:
        pinned = set(_lane(lane)["reps"])
        served = {
            rep
            for rep, profile in rep_profiles.items()
            if profile == _lane(lane)["profile"] and rep != "smoke"
        }
        assert pinned == served, f"{lane}: pinned reps {pinned} != prefect {served}"

    unmapped: list[str] = []
    for rep, spec in reps.items():
        prompt_files = set(_PROMPT_FILE_RE.findall(spec["prompt"]))
        if rep == "smoke":
            assert not prompt_files
            continue
        assert prompt_files, f"rep {rep} names no prompt file"
        lane = lane_by_profile[rep_profiles[rep]]
        missing = prompt_files - set(_lane(lane)["prompt_files"])
        if missing:
            unmapped.append(f"{rep} -> {lane}: {sorted(missing)}")
    assert not unmapped, f"rep prompts not scanned for their lane: {unmapped}"


def test_required_tools_pass_the_rep_harness_allowlist() -> None:
    module = _prefect_sessions_module()
    allowed = {
        tool.removeprefix(_PREFECT_TOOL_PREFIX)
        for tool in _module_literal(module, "LIVE_ALLOWED_TOOLS")
        if tool.startswith(_PREFECT_TOOL_PREFIX)
    }
    assert len(allowed) >= 30, f"LIVE_ALLOWED_TOOLS parse too small: {len(allowed)}"
    denied = {
        f"{lane}/{tool}"
        for lane in LANES
        for tool in _classified(lane, "required")
        if tool not in allowed
    }
    assert not denied, (
        f"prompt-required tools the rep harness denies (prefect LIVE_ALLOWED_TOOLS): "
        f"{sorted(denied)}"
    )
