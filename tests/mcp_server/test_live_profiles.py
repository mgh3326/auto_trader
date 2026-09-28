"""Task #891 / Q-52 (2026-09-28): live-* profile manifest contract tests.

The live-kr / live-us / live-crypto profiles are closed-world surfaces whose
membership lives in exactly one operator-reviewed file:
``config/mcp_profiles/live.yaml`` (core + extended tiers).

These tests pin, per profile:

* the registered tool set equals the manifest selection exactly;
* the manifest selection is a strict subset of the DEFAULT registered surface;
* no live ``*_place_order`` / ``*_modify_order`` / ``*_cancel_order`` (or any
  direct broker mutation / reconcile / proposal-lifecycle / harness-denied
  tool) is reachable;
* write tools are the operator-draft set, or EXTENDED-only with doc evidence;
* lane allowlists and the route_request lane taxonomy stay consistent;
* a manifest name no registrar produces fails registration (startup/test time);
* a manifest name outside the route taxonomy fails at manifest load.

The assertion-RED mutants at the bottom prove the subset and no-order
assertions really fire.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import register_all_tools
from app.mcp_server.tooling.live_profile_registration import (
    LIVE_MANIFEST_PATH,
    LIVE_PROFILES,
    OPERATOR_DRAFT_WRITE_TOOLS,
    LiveProfileManifest,
    load_live_manifest,
)
from app.mcp_server.tooling.route_request_lanes import (
    ALL_KNOWN_TOOLS,
    DIRECT_BROKER_MUTATION_TOOLS,
    HARNESS_DENIED_TOOLS,
    INTENT_TO_LANE,
    MUTATION_TOOLS,
    RECONCILE_TOOLS,
)
from tests.mcp_server._registration_recorder import (
    RegistrationRecorder,
    collect_profile_tools,
)

pytestmark = pytest.mark.unit

ALLOWLIST_DIR = Path(__file__).resolve().parents[2] / "config" / "mcp_lane_allowlists"

# Named DB-writer tools outside the route mutation taxonomy (mirrors the
# reviewed set in test_lane_allowlist_contract.py).
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
        "investment_watch_create",
        "sweep_expired_watches",
    }
)

# Read-only status helpers that sit inside MUTATION_TOOLS by taxonomy but
# perform no broker/account mutation.
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
        "decision_table_validate",
        "get_intraday_investor_flow",
        "get_upbit_altseason",
    }
)

_LIVE_WRITE_TOOLS = frozenset(
    (MUTATION_TOOLS | _HANDLER_WRITE_TOOL_NAMES) - _MUTATION_BUCKET_READS
)

# Order-mutation name pattern (a superset tripwire for unclassified names).
_LIVE_ORDER_NAME_RE = re.compile(
    r"(place|modify|cancel|submit)_order|cancel_pending_order|execute_report|reconcile"
)

# Manifest tools absent from every audited lane allowlist in
# config/mcp_lane_allowlists — each entry is a deliberate, doc-evidenced
# addition (hk:doc report/2026-09-28/q52-mcp-usage-30d):
#   execution_ledger_fill_events_list_recent — 99 calls/30d; fill-event read.
#   investment_watch_void — 6 calls/30d; operator draft write (watch lifecycle).
_MANIFEST_TOOLS_WITHOUT_LANE_AUDIT = frozenset(
    {"execution_ledger_fill_events_list_recent", "investment_watch_void"}
)

_LIVE_PROFILE_VALUES = sorted(profile.value for profile in LIVE_PROFILES)


def _manifest() -> LiveProfileManifest:
    return load_live_manifest()


def _registered(profile: McpProfile) -> set[str]:
    recorder = RegistrationRecorder()
    register_all_tools(cast(Any, recorder), profile=profile)
    return set(recorder.tools)


def _selected(profile: McpProfile) -> set[str]:
    return set(_manifest().spec_for(profile).selected_tool_names())


def _assert_strict_subset(manifest_tools: set[str], registered: set[str]) -> None:
    assert manifest_tools <= registered, (
        f"manifest tools absent from registration: "
        f"{sorted(manifest_tools - registered)}"
    )
    assert manifest_tools != registered


def _assert_registered_equals_manifest(
    registered: set[str], manifest_tools: set[str]
) -> None:
    assert registered == manifest_tools, (
        f"registered != manifest: "
        f"extra={sorted(registered - manifest_tools)} "
        f"missing={sorted(manifest_tools - registered)}"
    )


def _assert_no_live_order_tools(tool_names: set[str]) -> None:
    assert not (tool_names & DIRECT_BROKER_MUTATION_TOOLS), (
        f"direct broker mutations reachable: "
        f"{sorted(tool_names & DIRECT_BROKER_MUTATION_TOOLS)}"
    )
    assert not (tool_names & RECONCILE_TOOLS)
    assert not (tool_names & HARNESS_DENIED_TOOLS)
    assert not [name for name in tool_names if _LIVE_ORDER_NAME_RE.search(name)]


def _audited_lane_union() -> set[str]:
    union: set[str] = set()
    for path in ALLOWLIST_DIR.glob("*.txt"):
        for line in path.read_text().splitlines():
            if line and not line.startswith("#"):
                union.add(line.split("\t")[0])
    return union


class TestManifestSchema:
    def test_manifest_file_exists_at_canonical_path(self) -> None:
        assert LIVE_MANIFEST_PATH.is_file()
        assert LIVE_MANIFEST_PATH.name == "live.yaml"
        assert LIVE_MANIFEST_PATH.parent.name == "mcp_profiles"

    def test_manifest_covers_exactly_the_live_profiles(self) -> None:
        manifest = _manifest()
        assert set(manifest.specs) == set(LIVE_PROFILES)
        assert _LIVE_PROFILE_VALUES == ["live-crypto", "live-kr", "live-us"]

    def test_every_entry_has_tier_count_and_purpose(self) -> None:
        manifest = _manifest()
        for spec in manifest.specs.values():
            assert spec.tiers <= {"core", "extended"}
            assert spec.tiers, f"{spec.profile.value} selects no tiers"
            for entry in spec.tools:
                assert entry.tier in {"core", "extended"}
                assert entry.calls_30d >= 0
                assert entry.purpose

    def test_unknown_manifest_keys_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "profiles": {
                        profile: {
                            "tiers": ["core"],
                            "tools": [
                                {
                                    "name": "get_quote",
                                    "tier": "core",
                                    "calls_30d": 1,
                                    "purpose": "x",
                                    "bogus_key": True,
                                }
                            ],
                        }
                        for profile in _LIVE_PROFILE_VALUES
                    },
                }
            )
        )
        with pytest.raises(ValueError, match="unknown keys"):
            load_live_manifest(bad)

    def test_unclassified_tool_name_rejected_at_load(self, tmp_path: Path) -> None:
        """A name that is not a classified registered tool fails at load."""
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "profiles": {
                        profile: {
                            "tiers": ["core"],
                            "tools": [
                                {
                                    "name": "totally_made_up_tool",
                                    "tier": "core",
                                    "calls_30d": 1,
                                    "purpose": "x",
                                }
                            ],
                        }
                        for profile in _LIVE_PROFILE_VALUES
                    },
                }
            )
        )
        with pytest.raises(ValueError, match="not a classified registered tool"):
            load_live_manifest(bad)

    def test_forbidden_order_tool_rejected_at_load(self, tmp_path: Path) -> None:
        """A direct broker mutation in the manifest is refused at load."""
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "profiles": {
                        profile: {
                            "tiers": ["core"],
                            "tools": [
                                {
                                    "name": "kis_live_place_order",
                                    "tier": "core",
                                    "calls_30d": 1,
                                    "purpose": "x",
                                }
                            ],
                        }
                        for profile in _LIVE_PROFILE_VALUES
                    },
                }
            )
        )
        with pytest.raises(ValueError, match="forbidden on live profiles"):
            load_live_manifest(bad)

    def test_empty_tier_selection_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "profiles": {
                        profile: {"tiers": [], "tools": []}
                        for profile in _LIVE_PROFILE_VALUES
                    },
                }
            )
        )
        with pytest.raises(ValueError, match="tiers"):
            load_live_manifest(bad)


class TestManifestEqualsRegistry:
    """The registered live surface must equal the manifest selection exactly."""

    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_registered_equals_manifest_all_gates_on(
        self, monkeypatch: pytest.MonkeyPatch, profile: McpProfile
    ) -> None:
        inventories = collect_profile_tools(monkeypatch, gates_enabled=True)
        _assert_registered_equals_manifest(
            set(inventories[profile.value]), _selected(profile)
        )

    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_registered_equals_manifest_all_gates_off(
        self, monkeypatch: pytest.MonkeyPatch, profile: McpProfile
    ) -> None:
        inventories = collect_profile_tools(monkeypatch, gates_enabled=False)
        gated = {
            entry.name
            for entry in _manifest().spec_for(profile).selected_entries()
            if entry.gate is not None
        }
        assert gated, f"{profile.value}: expected at least one gated tool"
        _assert_registered_equals_manifest(
            set(inventories[profile.value]), _selected(profile) - gated
        )

    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_tier_switch_core_only(
        self, monkeypatch: pytest.MonkeyPatch, profile: McpProfile, tmp_path: Path
    ) -> None:
        """The profile's tiers field is the single switch for which tiers load."""
        raw = yaml.safe_load(LIVE_MANIFEST_PATH.read_text())
        raw["profiles"][profile.value]["tiers"] = ["core"]
        narrowed = tmp_path / "live.yaml"
        narrowed.write_text(yaml.safe_dump(raw))
        monkeypatch.setattr(
            "app.mcp_server.tooling.live_profile_registration.LIVE_MANIFEST_PATH",
            narrowed,
        )
        recorder = RegistrationRecorder()
        register_all_tools(cast(Any, recorder), profile=profile)
        manifest = load_live_manifest(narrowed)
        expected = {
            entry.name
            for entry in manifest.spec_for(profile).selected_entries()
            if entry.gate is None or getattr(settings, entry.gate, False)
        }
        assert set(recorder.tools) == expected
        assert set(recorder.tools) < _selected(profile)


class TestStrictSubsetAndNoOrderTools:
    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_manifest_is_strict_subset_of_default(
        self, monkeypatch: pytest.MonkeyPatch, profile: McpProfile
    ) -> None:
        default = set(
            collect_profile_tools(monkeypatch, gates_enabled=True)[
                McpProfile.DEFAULT.value
            ]
        )
        _assert_strict_subset(_selected(profile), default)

    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_no_live_order_tools_registered(
        self, monkeypatch: pytest.MonkeyPatch, profile: McpProfile
    ) -> None:
        registered = set(
            collect_profile_tools(monkeypatch, gates_enabled=True)[profile.value]
        )
        _assert_no_live_order_tools(registered)

    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_no_live_order_tools_in_manifest(self, profile: McpProfile) -> None:
        _assert_no_live_order_tools(
            set(_manifest().spec_for(profile).selected_tool_names())
        )

    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_write_tools_either_draft_or_extended(self, profile: McpProfile) -> None:
        spec = _manifest().spec_for(profile)
        core_writes = {
            entry.name
            for entry in spec.tools
            if entry.tier == "core" and entry.name in _LIVE_WRITE_TOOLS
        }
        assert core_writes <= OPERATOR_DRAFT_WRITE_TOOLS, (
            f"{profile.value}: non-draft write tools in CORE: "
            f"{sorted(core_writes - OPERATOR_DRAFT_WRITE_TOOLS)}"
        )
        all_writes = {
            entry.name for entry in spec.tools if entry.name in _LIVE_WRITE_TOOLS
        }
        assert all_writes <= OPERATOR_DRAFT_WRITE_TOOLS | {
            entry.name for entry in spec.tools if entry.tier == "extended"
        }


class TestLaneAndRouteConsistency:
    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_manifest_tools_classified_in_route_taxonomy(
        self, profile: McpProfile
    ) -> None:
        manifest_names = set(_manifest().spec_for(profile).selected_tool_names())
        assert manifest_names <= ALL_KNOWN_TOOLS

    def test_manifest_tools_have_lane_allowlist_coverage(self) -> None:
        """Every manifest tool appears in an audited config/mcp_lane_allowlists
        file, or is a named doc-evidenced exception."""
        union = _audited_lane_union()
        for profile in LIVE_PROFILES:
            uncovered = (
                set(_manifest().spec_for(profile).selected_tool_names())
                - union
                - _MANIFEST_TOOLS_WITHOUT_LANE_AUDIT
            )
            assert not uncovered, (
                f"{profile.value}: manifest tools with no audited lane "
                f"allowlist coverage and no named exception: {sorted(uncovered)}"
            )

    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    @pytest.mark.parametrize("market", ["kr", "us", "crypto"])
    @pytest.mark.parametrize("intent", sorted(INTENT_TO_LANE))
    @pytest.mark.asyncio
    async def test_route_plans_only_offer_registered_tools(
        self,
        monkeypatch: pytest.MonkeyPatch,
        profile: McpProfile,
        market: str,
        intent: str,
    ) -> None:
        monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
        recorder = RegistrationRecorder()
        register_all_tools(cast(Any, recorder), profile=profile)
        registered = set(recorder.tools)
        assert "route_request" in registered
        out = await recorder.tools["route_request"](intent=intent, market=market)
        assert isinstance(out.get("success"), bool)
        allowed = set(out["allowed_tools"])
        blocked = set(out["blocked_actions"])
        assert allowed <= registered, (
            f"{profile.value}/{intent}/{market}: route offers unregistered "
            f"tools: {sorted(allowed - registered)}"
        )
        assert not (allowed & DIRECT_BROKER_MUTATION_TOOLS)
        assert DIRECT_BROKER_MUTATION_TOOLS <= blocked | allowed, (
            f"{profile.value}/{intent}/{market}: broker mutations neither "
            "allowed nor blocked"
        )
        assert not (blocked - ALL_KNOWN_TOOLS), (
            f"{profile.value}/{intent}/{market}: unclassified tools blocked: "
            f"{sorted(blocked - ALL_KNOWN_TOOLS)}"
        )


class TestRegistrationFailureModes:
    """Startup/test-time failures for manifest/registry drift."""

    def test_manifest_tool_no_registrar_produces_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A classified, allowed tool that no live-path registrar emits (here:
        paper_list_pending_orders — registered only under the DEFAULT order
        branch the live path does not run) must fail registration."""
        raw = yaml.safe_load(LIVE_MANIFEST_PATH.read_text())
        raw["profiles"]["live-kr"]["tools"].append(
            {
                "name": "paper_list_pending_orders",
                "tier": "extended",
                "calls_30d": 0,
                "purpose": "mutant: exists in taxonomy but unwired for live",
            }
        )
        mutant = tmp_path / "live.yaml"
        mutant.write_text(yaml.safe_dump(raw))
        monkeypatch.setattr(
            "app.mcp_server.tooling.live_profile_registration.LIVE_MANIFEST_PATH",
            mutant,
        )
        with pytest.raises(ValueError, match="no registrar produced"):
            _registered(McpProfile.LIVE_KR)

    def test_live_kr_profile_does_not_leak_shared_registrars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tool loaded but absent from the manifest cannot register:
        run the shared Always block through the filter and confirm e.g.
        get_indicators / search_symbol / screen_stocks never land."""
        registered = set(
            collect_profile_tools(monkeypatch, gates_enabled=True)["live-kr"]
        )
        for absent in (
            "get_indicators",
            "search_symbol",
            "screen_stocks",
            "get_fx_rate",
            "session_bootstrap_pack",
            "get_trading_policy",
            "investment_watch_expire",
            "sweep_expired_watches",
            "proposal_revalidate",
            "order_proposal_void",
            "order_proposal_expire_sweep",
            "order_proposal_redispatch",
            "order_proposal_list_expired_defensive",
            "support_reserve_net_consume",
            "toss_get_positions",
            "toss_get_orderable_cash",
            "toss_preview_order",
            "toss_detect_manual_activity",
            "decision_table_apply",
            "live_reconcile_orders",
            "kis_live_get_order_history",
        ):
            assert absent not in registered


class TestAssertionRedMutants:
    """Directed mutants: prove the subset and no-order assertions catch."""

    def test_subset_assertion_catches_phantom_manifest_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        default = set(
            collect_profile_tools(monkeypatch, gates_enabled=True)[
                McpProfile.DEFAULT.value
            ]
        )
        mutant = _selected(McpProfile.LIVE_KR) | {"phantom_tool_xyz"}
        with pytest.raises(AssertionError):
            _assert_strict_subset(mutant, default)

    def test_equality_assertion_catches_tool_registered_outside_manifest(
        self,
    ) -> None:
        """Directed mutant: a registrar emits a tool the manifest does not
        list — the file==registry equality assertion must turn RED."""
        mutant_registered = _selected(McpProfile.LIVE_KR) | {"get_indicators"}
        with pytest.raises(AssertionError):
            _assert_registered_equals_manifest(
                mutant_registered, _selected(McpProfile.LIVE_KR)
            )

    def test_no_order_assertion_catches_live_mutation_leak(self) -> None:
        mutant = _selected(McpProfile.LIVE_KR) | {"kis_live_place_order"}
        with pytest.raises(AssertionError):
            _assert_no_live_order_tools(mutant)

    def test_no_order_assertion_catches_generic_mutation_leak(self) -> None:
        mutant = _selected(McpProfile.LIVE_US) | {"place_order"}
        with pytest.raises(AssertionError):
            _assert_no_live_order_tools(mutant)

    def test_no_order_assertion_catches_reconcile_leak(self) -> None:
        mutant = _selected(McpProfile.LIVE_CRYPTO) | {"live_reconcile_orders"}
        with pytest.raises(AssertionError):
            _assert_no_live_order_tools(mutant)

    def test_no_order_assertion_catches_harness_denied_leak(self) -> None:
        mutant = _selected(McpProfile.LIVE_KR) | {"kis_live_get_order_history"}
        with pytest.raises(AssertionError):
            _assert_no_live_order_tools(mutant)


class TestTierCounts:
    """Q-52: the file stays operator-readable; per-lane size stays small."""

    def test_per_lane_tier_counts(self) -> None:
        expected = {
            "live-kr": {"core": 15, "extended": 10},
            "live-us": {"core": 15, "extended": 10},
            "live-crypto": {"core": 15, "extended": 11},
        }
        manifest = _manifest()
        for profile_name, tiers in expected.items():
            spec = manifest.spec_for(McpProfile(profile_name))
            counts = {"core": 0, "extended": 0}
            for entry in spec.tools:
                counts[entry.tier] += 1
            assert counts == tiers, (
                f"{profile_name}: {counts} != {tiers} — edit the manifest "
                "deliberately via operator-approved PR"
            )
