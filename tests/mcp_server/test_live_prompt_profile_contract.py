"""Task #1003: live-session prompts vs the MCP surface they run on.

Incident 2026-09-29 22:35: us-2235 ran on the dedicated at-mcp-live-us unit,
which served exactly the ``live-us`` profile of the old live.yaml manifest
(29 tools). The prompt it executes required tools that profile did not serve,
and the session ended with 0 orders and 0 proposals.

ROB-1436 (2026-11-01) retired the live-* profiles and their units: live
sessions run in prefect ``KR_LIVE_MCP_MODE=shared`` on the DEFAULT surface.
The contract is now: every tool a live prompt REQUIRES as a step, or names as
served, must be registered on DEFAULT (gates on, as in production). The
prompts live in auto_trader-operator, so the referenced set is pinned in
``tests/fixtures/live_prompt_tool_requirements.yaml`` (operator commit +
file:line evidence), and:

* the always-run tests compare that pin to the DEFAULT registration — no
  checkout needed; a tool cleanup that unregisters a prompt-required tool
  fails here;
* with ``AUTO_TRADER_OPERATOR_ROOT`` set, the drift tests re-read the prompts
  and fail when a referenced tool is unclassified in the pin or a pinned ref
  no longer holds;
* with ``ROBIN_PREFECT_AUTOMATIONS_ROOT`` set, the rep -> lane mapping and the
  harness allowlist are checked against prefect.

The conditional / guarded / not_required classes stay recorded (they say how
a prompt uses a name) but are not enforced against the surface.
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


def _prose_served(lane: str) -> dict[str, Any]:
    """Served tools a prompt step describes in prose without naming them."""
    return _lane(lane)["prose_served"] or {}


def _pinned_tokens(lane: str) -> set[str]:
    """Tool names the pin records as literally referenced (served + classes)."""
    return _served(lane).union(*(_classified(lane, cls) for cls in CLASSES))


def _pinned(lane: str) -> set[str]:
    return _pinned_tokens(lane) | set(_prose_served(lane))


_SURFACE_CACHE: dict[str, frozenset[str]] = {}


def _profile_tools(lane: str) -> frozenset[str]:
    """The registered surface of the lane's profile, gates on (production)."""
    profile = McpProfile(_lane(lane)["profile"]).value
    if profile not in _SURFACE_CACHE:
        with pytest.MonkeyPatch.context() as monkeypatch:
            surfaces = collect_profile_tools(monkeypatch, gates_enabled=True)
        _SURFACE_CACHE[profile] = frozenset(surfaces[profile])
    return _SURFACE_CACHE[profile]


def _required_gap(lane: str) -> set[str]:
    return set(_classified(lane, "required")) - _profile_tools(lane)


# ---------------------------------------------------------------------------
# Always-run: pinned requirements vs the DEFAULT registration
# ---------------------------------------------------------------------------


class TestPinnedRequirementsShape:
    def test_lanes_and_profiles(self) -> None:
        data = _requirements()
        assert data["version"] == 1
        assert tuple(data["lanes"]) == LANES
        for lane in LANES:
            # ROB-1436: live sessions run on DEFAULT (prefect shared mode).
            assert _lane(lane)["profile"] == McpProfile.DEFAULT.value
            assert _lane(lane)["prefect_rep_profile"] == f"live-{lane}"
            assert _lane(lane)["reps"], lane
            assert _lane(lane)["prompt_files"], lane

    def test_classes_are_disjoint_and_complete(self) -> None:
        for lane in LANES:
            served = _lane(lane)["served"]
            assert served and len(served) == len(set(served)), lane
            seen: dict[str, str] = dict.fromkeys(served, "served")
            assert "prose_served" in _lane(lane), f"{lane}: missing prose_served"
            for tool, entry in _prose_served(lane).items():
                assert tool not in seen, f"{lane}: {tool} is served and prose_served"
                seen[tool] = "prose_served"
                assert entry["phrase"].strip(), f"{lane}/{tool}: empty phrase"
                assert entry["refs"], f"{lane}/{tool}: no refs"
                for ref in entry["refs"]:
                    assert _REF_RE.match(ref), f"{lane}/{tool}: bad ref {ref}"
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


@pytest.mark.parametrize("lane", LANES)
def test_served_references_stay_in_profile(lane: str) -> None:
    """A registration change may not drop a tool the lane prompts reference."""
    dropped = (_served(lane) | set(_prose_served(lane))) - _profile_tools(lane)
    assert not dropped, (
        f"DEFAULT no longer serves {lane} prompt-referenced tools {sorted(dropped)}; "
        f"either restore them or reclassify them in {REQUIREMENTS_PATH.name} "
        "with the operator decision"
    )


@pytest.mark.parametrize("lane", LANES)
def test_live_prompt_required_tools_are_registered(lane: str) -> None:
    gap = _required_gap(lane)
    assert not gap, (
        f"live prompts for lane {lane} require tools DEFAULT does not "
        f"register: {sorted(gap)}"
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
        f"each to served (if DEFAULT serves it) or classify it in "
        f"{REQUIREMENTS_PATH.name}: "
        f"{ {tool: refs[:4] for tool, refs in sorted(unpinned.items())} }"
    )


@pytest.mark.parametrize("lane", LANES)
def test_pinned_classifications_are_still_referenced(
    lane: str, tool_universe: set[str]
) -> None:
    root = _operator_root()
    extracted = _extract(root, lane, tool_universe)
    stale = sorted(_pinned_tokens(lane) - set(extracted))
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
    for tool, entry in _prose_served(lane).items():
        for ref in entry["refs"]:
            match = _REF_RE.match(ref)
            assert match, ref
            path = root / match["path"]
            lines = (
                path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
            )
            index = int(match["line"]) - 1
            if index >= len(lines) or entry["phrase"] not in lines[index]:
                drifted.append(f"{tool} (prose '{entry['phrase']}') @ {ref}")
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
    lane_by_profile = {_lane(lane)["prefect_rep_profile"]: lane for lane in LANES}

    for lane in LANES:
        pinned = set(_lane(lane)["reps"])
        served = {
            rep
            for rep, profile in rep_profiles.items()
            if profile == _lane(lane)["prefect_rep_profile"] and rep != "smoke"
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
