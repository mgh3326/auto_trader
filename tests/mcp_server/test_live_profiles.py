"""Task #891 / Q-53 (2026-09-28): live-* profile manifest contract tests.

The live-kr / live-us / live-crypto profiles are closed-world surfaces whose
membership lives in exactly one operator-reviewed file:
``config/mcp_profiles/live.yaml`` — three groups per lane (``core`` 15,
per-market ``extension`` up to 10, ``emergency`` recovery exceptions), all
loaded.

These tests pin, per profile:

* the registered tool set equals the manifest selection exactly;
* the manifest selection is a strict subset of the DEFAULT registered surface;
* mutation-class tools (``*_place_order`` / ``*_modify_order`` /
  ``*_cancel_order`` / reconcile) exist ONLY inside the ``emergency`` group
  and only from the named existing-tool list — ``kis_live_place_order`` and
  the harness-denied ``kis_live_get_order_history`` are never reachable;
* write tools outside the emergency group are the operator-draft set, or
  EXTENSION-only with doc evidence;
* lane allowlists and the route_request lane taxonomy stay consistent;
* a manifest name no registrar produces fails registration (startup/test time);
* a manifest name outside the route taxonomy fails at manifest load.

The assertion-RED mutants at the bottom prove the subset and
emergency-confinement assertions really fire.
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
    LIVE_EMERGENCY_TOOL_NAMES,
    LIVE_GROUP_CORE,
    LIVE_GROUP_EMERGENCY,
    LIVE_GROUP_EXTENSION,
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
# config/mcp_lane_allowlists — each entry is a deliberate, doc-evidenced or
# Q-53-named exception:
#   execution_ledger_fill_events_list_recent — 99 calls/30d; fill-event read.
#   investment_watch_void — watch-void emergency exception (Q-53).
#   kis_live_reconcile_orders — KR emergency reconcile (Q-53).
#   toss_modify_order — Toss KR emergency modify (Q-53).
_MANIFEST_TOOLS_WITHOUT_LANE_AUDIT = frozenset(
    {
        "execution_ledger_fill_events_list_recent",
        "investment_watch_void",
        "kis_live_reconcile_orders",
        "toss_modify_order",
    }
)

# Names that must NEVER appear on a live profile, even in the emergency
# group: loss_cut is disabled on every direct place path (ROB-864 — so
# place_order/toss_place_order would only grant unrestricted direct
# placement, verified tester r3), and kis_live_get_order_history is
# harness-denied (#678).
_LIVE_NEVER_TOOL_NAMES = frozenset(
    {
        "place_order",
        "toss_place_order",
        "kis_live_place_order",
        "kis_live_get_order_history",
    }
)

# Q-58: the only harness-denied tool permitted on a live profile —
# operator-approved for live-crypto's C1 breadth read.
_HARNESS_DENIED_ALLOWED = {
    McpProfile.LIVE_CRYPTO: frozenset({"get_upbit_altseason"}),
}

_LIVE_PROFILE_VALUES = sorted(profile.value for profile in LIVE_PROFILES)


def _manifest() -> LiveProfileManifest:
    return load_live_manifest()


def _registered(profile: McpProfile) -> set[str]:
    recorder = RegistrationRecorder()
    register_all_tools(cast(Any, recorder), profile=profile)
    return set(recorder.tools)


def _selected(profile: McpProfile) -> set[str]:
    return set(_manifest().spec_for(profile).selected_tool_names())


def _emergency(profile: McpProfile) -> set[str]:
    return set(_manifest().spec_for(profile).group_tool_names(LIVE_GROUP_EMERGENCY))


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


def _assert_mutations_confined_to_emergency(
    profile: McpProfile, tool_names: set[str], emergency_names: set[str]
) -> None:
    """Every mutation-class name present must be a named emergency entry."""
    mutationish = {
        name
        for name in tool_names
        if name in DIRECT_BROKER_MUTATION_TOOLS
        or name in RECONCILE_TOOLS
        or _LIVE_ORDER_NAME_RE.search(name)
        or name == "investment_watch_void"
    }
    assert emergency_names <= LIVE_EMERGENCY_TOOL_NAMES
    assert mutationish <= emergency_names <= LIVE_EMERGENCY_TOOL_NAMES, (
        f"mutation tools outside the named emergency set: "
        f"{sorted(mutationish - emergency_names)}"
    )
    allowed_denied = _HARNESS_DENIED_ALLOWED.get(profile, frozenset())
    assert not (tool_names & (HARNESS_DENIED_TOOLS - allowed_denied))
    assert not (tool_names & _LIVE_NEVER_TOOL_NAMES)


def _audited_lane_union() -> set[str]:
    union: set[str] = set()
    for path in ALLOWLIST_DIR.glob("*.txt"):
        for line in path.read_text().splitlines():
            if line and not line.startswith("#"):
                union.add(line.split("\t")[0])
    return union


def _mini_manifest(groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "version": 1,
        "profiles": {profile: {"groups": groups} for profile in _LIVE_PROFILE_VALUES},
    }


class TestManifestSchema:
    def test_manifest_file_exists_at_canonical_path(self) -> None:
        assert LIVE_MANIFEST_PATH.is_file()
        assert LIVE_MANIFEST_PATH.name == "live.yaml"
        assert LIVE_MANIFEST_PATH.parent.name == "mcp_profiles"

    def test_manifest_covers_exactly_the_live_profiles(self) -> None:
        manifest = _manifest()
        assert set(manifest.specs) == set(LIVE_PROFILES)
        assert _LIVE_PROFILE_VALUES == ["live-crypto", "live-kr", "live-us"]

    def test_every_entry_has_group_and_purpose(self) -> None:
        manifest = _manifest()
        for spec in manifest.specs.values():
            for entry in spec.tools:
                assert entry.group in {
                    LIVE_GROUP_CORE,
                    LIVE_GROUP_EXTENSION,
                    LIVE_GROUP_EMERGENCY,
                }
                assert entry.calls_30d is None or entry.calls_30d >= 0
                if entry.group != LIVE_GROUP_EMERGENCY:
                    assert entry.calls_30d is not None
                assert entry.purpose

    def test_unknown_manifest_keys_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                _mini_manifest(
                    {
                        "core": [
                            {
                                "name": "get_quote",
                                "calls_30d": 1,
                                "purpose": "x",
                                "bogus_key": True,
                            }
                        ],
                        "extension": [
                            {"name": "get_news", "calls_30d": 1, "purpose": "x"}
                        ],
                        "emergency": [{"name": "cancel_order", "purpose": "x"}],
                    }
                )
            )
        )
        with pytest.raises(ValueError, match="unknown keys"):
            load_live_manifest(bad)

    def test_missing_group_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                _mini_manifest(
                    {
                        "core": [{"name": "get_quote", "calls_30d": 1, "purpose": "x"}],
                        "extension": [
                            {"name": "get_news", "calls_30d": 1, "purpose": "x"}
                        ],
                    }
                )
            )
        )
        with pytest.raises(ValueError, match="groups must be exactly"):
            load_live_manifest(bad)

    def test_unclassified_tool_name_rejected_at_load(self, tmp_path: Path) -> None:
        """A name that is not a classified registered tool fails at load."""
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                _mini_manifest(
                    {
                        "core": [
                            {
                                "name": "totally_made_up_tool",
                                "calls_30d": 1,
                                "purpose": "x",
                            }
                        ],
                        "extension": [
                            {"name": "get_news", "calls_30d": 1, "purpose": "x"}
                        ],
                        "emergency": [{"name": "cancel_order", "purpose": "x"}],
                    }
                )
            )
        )
        with pytest.raises(ValueError, match="not a classified registered tool"):
            load_live_manifest(bad)

    def test_mutation_tool_in_core_rejected_at_load(self, tmp_path: Path) -> None:
        """place_order is an emergency-only tool; in core it is refused."""
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                _mini_manifest(
                    {
                        "core": [
                            {"name": "place_order", "calls_30d": 1, "purpose": "x"}
                        ],
                        "extension": [
                            {"name": "get_news", "calls_30d": 1, "purpose": "x"}
                        ],
                        "emergency": [{"name": "cancel_order", "purpose": "x"}],
                    }
                )
            )
        )
        with pytest.raises(ValueError, match="emergency"):
            load_live_manifest(bad)

    def test_unlisted_mutation_in_emergency_rejected_at_load(
        self, tmp_path: Path
    ) -> None:
        """kis_live_place_order is not a named emergency tool (ROB-864
        disables loss_cut on it) — refused even inside emergency."""
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                _mini_manifest(
                    {
                        "core": [{"name": "get_quote", "calls_30d": 1, "purpose": "x"}],
                        "extension": [
                            {"name": "get_news", "calls_30d": 1, "purpose": "x"}
                        ],
                        "emergency": [{"name": "kis_live_place_order", "purpose": "x"}],
                    }
                )
            )
        )
        with pytest.raises(ValueError, match="forbidden on live profiles"):
            load_live_manifest(bad)

    def test_harness_denied_tool_in_emergency_rejected_at_load(
        self, tmp_path: Path
    ) -> None:
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                _mini_manifest(
                    {
                        "core": [{"name": "get_quote", "calls_30d": 1, "purpose": "x"}],
                        "extension": [
                            {"name": "get_news", "calls_30d": 1, "purpose": "x"}
                        ],
                        "emergency": [
                            {"name": "kis_live_get_order_history", "purpose": "x"}
                        ],
                    }
                )
            )
        )
        with pytest.raises(ValueError, match="forbidden on live profiles"):
            load_live_manifest(bad)

    def test_core_cap_enforced(self, tmp_path: Path) -> None:
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                _mini_manifest(
                    {
                        "core": [
                            {"name": f"tool_{i:02d}", "calls_30d": 0, "purpose": "x"}
                            for i in range(16)
                        ],
                        "extension": [
                            {"name": "get_news", "calls_30d": 1, "purpose": "x"}
                        ],
                        "emergency": [{"name": "cancel_order", "purpose": "x"}],
                    }
                )
            )
        )
        with pytest.raises(ValueError, match="caps core at 15"):
            load_live_manifest(bad)

    def test_extension_cap_enforced(self, tmp_path: Path) -> None:
        bad = tmp_path / "live.yaml"
        bad.write_text(
            yaml.safe_dump(
                _mini_manifest(
                    {
                        "core": [{"name": "get_quote", "calls_30d": 1, "purpose": "x"}],
                        "extension": [
                            {"name": f"tool_{i:02d}", "calls_30d": 0, "purpose": "x"}
                            for i in range(11)
                        ],
                        "emergency": [{"name": "cancel_order", "purpose": "x"}],
                    }
                )
            )
        )
        with pytest.raises(ValueError, match="caps extension at 10"):
            load_live_manifest(bad)

    def test_harness_denied_altseason_rejected_off_crypto(self, tmp_path: Path) -> None:
        """Q-58 scoped the exception to live-crypto — the same manifest line
        must fail on live-kr / live-us."""
        for denied_profile in ("live-kr", "live-us"):
            profiles = {
                name: {
                    "groups": {
                        "core": [{"name": "get_quote", "calls_30d": 1, "purpose": "x"}],
                        "extension": [
                            {"name": "get_news", "calls_30d": 1, "purpose": "x"},
                            {
                                "name": "get_upbit_altseason",
                                "calls_30d": 0,
                                "purpose": "x",
                            },
                        ],
                        "emergency": [{"name": "cancel_order", "purpose": "x"}],
                    }
                }
                for name in _LIVE_PROFILE_VALUES
            }
            # Only the denied lane carries the tool; the others stay clean.
            for name in _LIVE_PROFILE_VALUES:
                if name != denied_profile:
                    profiles[name]["groups"]["extension"].pop()
            bad = tmp_path / f"live-{denied_profile}.yaml"
            bad.write_text(yaml.safe_dump({"version": 1, "profiles": profiles}))
            with pytest.raises(ValueError, match="forbidden on live profiles"):
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


class TestStrictSubsetAndEmergencyConfinement:
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
    def test_registered_mutations_are_only_emergency(
        self, monkeypatch: pytest.MonkeyPatch, profile: McpProfile
    ) -> None:
        registered = set(
            collect_profile_tools(monkeypatch, gates_enabled=True)[profile.value]
        )
        _assert_mutations_confined_to_emergency(
            profile, registered, _emergency(profile)
        )

    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_manifest_mutations_are_only_emergency(self, profile: McpProfile) -> None:
        _assert_mutations_confined_to_emergency(
            profile, _selected(profile), _emergency(profile)
        )

    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_write_tools_either_draft_or_extension_or_emergency(
        self, profile: McpProfile
    ) -> None:
        spec = _manifest().spec_for(profile)
        core_writes = {
            entry.name
            for entry in spec.tools
            if entry.group == LIVE_GROUP_CORE and entry.name in _LIVE_WRITE_TOOLS
        }
        assert core_writes <= OPERATOR_DRAFT_WRITE_TOOLS, (
            f"{profile.value}: non-draft write tools in CORE: "
            f"{sorted(core_writes - OPERATOR_DRAFT_WRITE_TOOLS)}"
        )
        all_writes = {
            entry.name for entry in spec.tools if entry.name in _LIVE_WRITE_TOOLS
        }
        permitted_extra = {
            entry.name
            for entry in spec.tools
            if entry.group in {LIVE_GROUP_EXTENSION, LIVE_GROUP_EMERGENCY}
        }
        assert all_writes <= OPERATOR_DRAFT_WRITE_TOOLS | permitted_extra


class TestLaneAndRouteConsistency:
    @pytest.mark.parametrize("profile", sorted(LIVE_PROFILES, key=str))
    def test_manifest_tools_classified_in_route_taxonomy(
        self, profile: McpProfile
    ) -> None:
        manifest_names = set(_manifest().spec_for(profile).selected_tool_names())
        assert manifest_names <= ALL_KNOWN_TOOLS

    def test_manifest_tools_have_lane_allowlist_coverage(self) -> None:
        """Every manifest tool appears in an audited config/mcp_lane_allowlists
        file, or is a named doc-evidenced/Q-53 exception."""
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
        # Emergency mutations may be advertised only when actually registered.
        assert (allowed & DIRECT_BROKER_MUTATION_TOOLS) <= _emergency(profile)
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
        raw["profiles"]["live-kr"]["groups"]["extension"].append(
            {
                "name": "paper_list_pending_orders",
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
            # Proposal lifecycle and exit-planning tools stay off live
            # surfaces: operator Q-62 fixed the emergency group to
            # cancel/modify/reconcile/watch-void only.
            "proposal_revalidate",
            "order_proposal_void",
            "order_proposal_expire_sweep",
            "order_proposal_redispatch",
            "order_proposal_list_expired_defensive",
            "sell_ladder_fill_preview",
            "support_reserve_net_consume",
            "toss_get_positions",
            "toss_get_orderable_cash",
            "toss_preview_order",
            "toss_detect_manual_activity",
            "decision_table_apply",
            "live_reconcile_orders",
            # ROB-864: loss_cut is disabled on every direct place path, so
            # they are not loss_cut tools — listing them would only grant
            # unrestricted direct placement (tester r3 finding).
            "place_order",
            "toss_place_order",
            "kis_live_place_order",
            "kis_live_get_order_history",
            "kis_mock_reconciliation_run",
        ):
            assert absent not in registered

    @pytest.mark.parametrize("profile", [McpProfile.LIVE_US, McpProfile.LIVE_CRYPTO])
    def test_non_kr_live_profiles_drop_broker_typed_tools(
        self, monkeypatch: pytest.MonkeyPatch, profile: McpProfile
    ) -> None:
        """KR-broker-typed tools never land on the US/crypto lanes."""
        registered = set(
            collect_profile_tools(monkeypatch, gates_enabled=True)[profile.value]
        )
        for absent in (
            "kis_live_place_order",
            "kis_live_cancel_order",
            "kis_live_modify_order",
            "kis_live_reconcile_orders",
            "kis_live_get_order_history",
            "toss_place_order",
            "toss_cancel_order",
            "toss_modify_order",
            "toss_reconcile_orders",
            "toss_get_order_history",
        ):
            assert absent not in registered


class TestAssertionRedMutants:
    """Directed mutants: prove the subset and emergency assertions catch."""

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

    def test_emergency_assertion_catches_place_leak(self) -> None:
        """place_order is not an emergency-eligible name on any lane — a
        leak must turn RED (it cannot do loss_cut; ROB-864)."""
        mutant = _selected(McpProfile.LIVE_KR) | {"place_order"}
        with pytest.raises(AssertionError):
            _assert_mutations_confined_to_emergency(
                McpProfile.LIVE_KR, mutant, _emergency(McpProfile.LIVE_KR)
            )

    def test_emergency_assertion_catches_unlisted_mutation_leak(self) -> None:
        """A mutation registered but not declared emergency must turn RED —
        alpaca_paper_submit_order is mutation-class but never live-eligible."""
        mutant = _selected(McpProfile.LIVE_US) | {"alpaca_paper_submit_order"}
        with pytest.raises(AssertionError):
            _assert_mutations_confined_to_emergency(
                McpProfile.LIVE_US, mutant, _emergency(McpProfile.LIVE_US)
            )

    def test_emergency_assertion_catches_reconcile_leak(self) -> None:
        """kis_live_reconcile_orders on the crypto lane is not in that lane's
        emergency set — must turn RED."""
        mutant = _selected(McpProfile.LIVE_CRYPTO) | {"kis_live_reconcile_orders"}
        with pytest.raises(AssertionError):
            _assert_mutations_confined_to_emergency(
                McpProfile.LIVE_CRYPTO, mutant, _emergency(McpProfile.LIVE_CRYPTO)
            )

    def test_emergency_assertion_catches_harness_denied_leak(self) -> None:
        mutant = _selected(McpProfile.LIVE_KR) | {"kis_live_get_order_history"}
        with pytest.raises(AssertionError):
            _assert_mutations_confined_to_emergency(
                McpProfile.LIVE_KR, mutant, _emergency(McpProfile.LIVE_KR)
            )

    def test_emergency_assertion_catches_off_lane_altseason_leak(self) -> None:
        """get_upbit_altseason is Q-58-approved ONLY on live-crypto — a leak
        onto live-kr must turn RED (it stays harness-denied there)."""
        mutant = _selected(McpProfile.LIVE_KR) | {"get_upbit_altseason"}
        with pytest.raises(AssertionError):
            _assert_mutations_confined_to_emergency(
                McpProfile.LIVE_KR, mutant, _emergency(McpProfile.LIVE_KR)
            )


class TestGroupCounts:
    """Q-53: core 15 / extension up to 10 / named emergency set per lane."""

    def test_per_lane_group_counts(self) -> None:
        expected = {
            "live-kr": {"core": 15, "extension": 9, "emergency": 9},
            "live-us": {"core": 15, "extension": 9, "emergency": 4},
            "live-crypto": {"core": 15, "extension": 10, "emergency": 4},
        }
        manifest = _manifest()
        for profile_name, groups in expected.items():
            spec = manifest.spec_for(McpProfile(profile_name))
            counts = {group: len(spec.group_entries(group)) for group in groups}
            assert counts == groups, (
                f"{profile_name}: {counts} != {groups} — edit the manifest "
                "deliberately via operator-approved PR"
            )
