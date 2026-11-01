"""Tests for MCP profile-driven tool registration.

Verifies that:
- DEFAULT profile registers legacy ambiguous order tools AND typed variants.
- HERMES_PAPER_KIS profile registers only kis_mock_* order tools; live surface absent.
- resolve_mcp_profile handles None/empty/valid/invalid inputs correctly.
"""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest

from app.core.config import settings
from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling.alpaca_paper import ALPACA_PAPER_READONLY_TOOL_NAMES
from app.mcp_server.tooling.alpaca_paper_automated_orders import (
    ALPACA_PAPER_AUTOMATED_TOOL_NAMES,
)
from app.mcp_server.tooling.alpaca_paper_orders import (
    ALPACA_PAPER_MUTATING_TOOL_NAMES,
)
from app.mcp_server.tooling.alpaca_paper_preview import ALPACA_PAPER_PREVIEW_TOOL_NAMES
from app.mcp_server.tooling.buy_gate_ab_shadow_v2_registration import (
    BUY_GATE_AB_SHADOW_V2_TOOL_NAMES,
)
from app.mcp_server.tooling.kiwoom_kr_registration import (
    KIWOOM_KR_EXCLUDED_US_MUTATION_TOOL_NAMES,
    KIWOOM_KR_TOOL_NAMES,
    kiwoom_kr_profile_tool_names,
)
from app.mcp_server.tooling.market_quote_snapshot_tools import (
    MARKET_QUOTE_SNAPSHOT_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
    LIVE_RECONCILE_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_us_variants import (
    KIWOOM_MOCK_US_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.orders_toss_variants import (
    TOSS_LIVE_ORDER_TOOL_NAMES,
)
from app.mcp_server.tooling.paper_account_registration import PAPER_ACCOUNT_TOOL_NAMES
from app.mcp_server.tooling.paper_limit_order_handler import (
    PAPER_LIMIT_ORDER_TOOL_NAMES,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.us_dual_paper import US_DUAL_PAPER_TOOL_NAMES
from tests._mcp_tooling_support import DummyMCP

PAPER_EXECUTION_TOOL_NAMES = {
    "paper_execution_get_order",
    "paper_execution_preview_order",
    "paper_execution_get_capabilities",
    "paper_execution_reconcile",
    "paper_execution_submit_order",
    "paper_execution_cancel_order",
}


_LEGACY_ORDER_TOOL_NAMES = ORDER_TOOL_NAMES  # {place_order, cancel_order, ...}
_ALPACA_PAPER_TOOL_NAMES = (
    ALPACA_PAPER_READONLY_TOOL_NAMES
    | ALPACA_PAPER_PREVIEW_TOOL_NAMES
    | ALPACA_PAPER_MUTATING_TOOL_NAMES
    | MARKET_QUOTE_SNAPSHOT_TOOL_NAMES
)
_US_PAPER_TOOL_NAMES = _ALPACA_PAPER_TOOL_NAMES | US_DUAL_PAPER_TOOL_NAMES
_DB_PAPER_TOOL_NAMES = PAPER_ACCOUNT_TOOL_NAMES
_CRYPTO_RESEARCH_TOOL_NAMES = {
    "get_crypto_profile",
    "get_kimchi_premium",
    "get_crypto_funding_rate",
    "get_crypto_open_interest",
    "get_crypto_long_short_ratio",
    "get_crypto_market_regime",
    "get_crypto_catalysts",
    "get_crypto_order_flow",
    "get_crypto_social",
    "get_upbit_index",
    "get_upbit_altseason",
    "get_crypto_fear_greed",
    "get_crypto_top_movers",
}

# ROB-503: generic 이름은 제거됨 (crypto-only 구현인데 이름이 시장 비특정).
# get_fear_greed_index는 ROB-488에서 get_crypto_fear_greed로 리네임.
_REMOVED_GENERIC_TOOL_NAMES = {
    "get_fear_greed_index",
    "get_funding_rate",
    "get_open_interest",
    "get_long_short_ratio",
}

_EXPECTED_KIWOOM_EXECUTION_TOOL_NAMES = {
    "kiwoom_mock_preview_order",
    "kiwoom_mock_place_order",
    "kiwoom_mock_cancel_order",
    "kiwoom_mock_modify_order",
    "kiwoom_mock_get_order_history",
    "kiwoom_mock_get_positions",
    "kiwoom_mock_get_orderable_cash",
}


def _build_mcp(profile: McpProfile) -> DummyMCP:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=profile)
    return mcp


class TestDefaultProfile:
    def test_registers_v2_buy_gate_witness_evaluator(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert BUY_GATE_AB_SHADOW_V2_TOOL_NAMES <= mcp.tools.keys()

    def test_registers_legacy_order_tools(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert _LEGACY_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_registers_typed_kis_live_variants(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert KIS_LIVE_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_registers_typed_kis_mock_variants(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert KIS_MOCK_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_registers_typed_toss_live_variants(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert TOSS_LIVE_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_does_not_register_split_profile_tools(self) -> None:
        # US/DB paper surfaces are profile-isolated and never appear in DEFAULT.
        # kiwoom_mock_* is flag-gated in DEFAULT (ROB-601) — its presence/absence
        # is owned by ``TestKiwoomDefaultProfileGate``, not this assertion.
        mcp = _build_mcp(McpProfile.DEFAULT)
        split_only = _US_PAPER_TOOL_NAMES | _DB_PAPER_TOOL_NAMES
        assert split_only.isdisjoint(mcp.tools.keys())


class TestAlpacaPaperPreviewProfile:
    def test_preview_tool_registered_us_paper_profile(self) -> None:
        mcp = _build_mcp(McpProfile.US_PAPER)
        assert "alpaca_paper_preview_order" in mcp.tools

    def test_preview_tool_not_registered_default_profile(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert "alpaca_paper_preview_order" not in mcp.tools

    def test_preview_tool_not_registered_hermes_paper_kis_profile(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        assert "alpaca_paper_preview_order" not in mcp.tools


class TestHermesPaperKisProfile:
    def test_does_not_register_legacy_order_tools(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        for name in _LEGACY_ORDER_TOOL_NAMES:
            assert name not in mcp.tools, (
                f"hermes-paper-kis must not register legacy tool '{name}'"
            )

    def test_does_not_register_live_order_tools(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        for name in KIS_LIVE_ORDER_TOOL_NAMES:
            assert name not in mcp.tools, (
                f"hermes-paper-kis must not register live tool '{name}'"
            )

    def test_registers_kis_mock_order_tools(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        assert KIS_MOCK_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_registers_readonly_research_tools(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        # Representative read-only tools that must be present in paper profile
        expected_readonly = {"get_quote", "get_holdings", "get_cash_balance"}
        for name in expected_readonly:
            assert name in mcp.tools, (
                f"hermes-paper-kis must register read-only tool '{name}'"
            )

    def test_does_not_register_alpaca_paper_tools(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        assert _ALPACA_PAPER_TOOL_NAMES.isdisjoint(mcp.tools.keys())


class TestUsPaperProfile:
    def test_registers_us_paper_tools(self) -> None:
        mcp = _build_mcp(McpProfile.US_PAPER)
        assert _US_PAPER_TOOL_NAMES <= mcp.tools.keys()


class TestAlpacaCleanProfile:
    def test_is_closed_world_and_contains_only_clean_alpaca_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "alpaca_paper_crypto_enabled", True)
        mcp = _build_mcp(McpProfile.ALPACA_PAPER_CLEAN)

        assert len(mcp.tools) == 14
        assert set(mcp.tools) == (
            ALPACA_PAPER_READONLY_TOOL_NAMES
            | ALPACA_PAPER_PREVIEW_TOOL_NAMES
            | {
                "session_bootstrap_pack",
                "alpaca_paper_ledger_list_recent",
                "alpaca_paper_ledger_get",
                "alpaca_paper_ledger_get_by_correlation",
                "alpaca_paper_roundtrip_report",
                "alpaca_paper_execution_preflight_check",
            }
        )
        assert "kis_mock_mirror_execute_report" not in mcp.tools
        assert _ALL_ORDER_TOOL_NAMES.isdisjoint(mcp.tools)

    def test_pins_account_mode_and_rejects_other_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "alpaca_paper_crypto_enabled", True)
        mcp = _build_mcp(McpProfile.ALPACA_PAPER_CLEAN)
        get_account = mcp.tools["alpaca_paper_get_account"]

        assert (
            inspect.signature(get_account).parameters["account_mode"].default
            == "alpaca_paper_crypto"
        )
        with pytest.raises(ValueError, match="pinned to account_mode"):
            import asyncio

            asyncio.run(get_account(account_mode="alpaca_paper"))

        preview = mcp.tools["alpaca_paper_preview_order"]
        with pytest.raises(ValueError, match="supports crypto orders only"):
            asyncio.run(
                preview(
                    symbol="AAPL",
                    side="buy",
                    type="limit",
                    qty=1,
                    limit_price=100,
                )
            )
        with pytest.raises(ValueError, match="crypto symbol must be one of"):
            asyncio.run(
                preview(
                    symbol="XRP/USD",
                    side="buy",
                    type="limit",
                    qty=1,
                    limit_price=1,
                    asset_class="crypto",
                )
            )


class TestDbPaperProfile:
    def test_registers_db_paper_tools(self) -> None:
        mcp = _build_mcp(McpProfile.DB_PAPER)
        assert _DB_PAPER_TOOL_NAMES <= mcp.tools.keys()


class TestCryptoProfile:
    def test_keeps_generic_research_surface(self) -> None:
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert {"get_quote", "screen_stocks", "get_holdings"} <= mcp.tools.keys()

    def test_keeps_crypto_discovery_tool(self) -> None:
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert "get_crypto_top_movers" in mcp.tools

    def test_registers_crypto_trading_surface(self) -> None:
        # A crypto session must be able to trade and settle: generic
        # account_mode order tools are the only Upbit entry point and
        # live_reconcile_orders is the US/crypto settle path.
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert _LEGACY_ORDER_TOOL_NAMES <= mcp.tools.keys()
        assert LIVE_RECONCILE_TOOL_NAMES <= mcp.tools.keys()

    def test_does_not_register_kis_typed_order_tools(self) -> None:
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert KIS_LIVE_ORDER_TOOL_NAMES.isdisjoint(mcp.tools.keys())
        assert KIS_MOCK_ORDER_TOOL_NAMES.isdisjoint(mcp.tools.keys())


class TestKiwoomProfile:
    def test_registers_kiwoom_mock_tools(self) -> None:
        mcp = _build_mcp(McpProfile.KIWOOM)
        assert KIWOOM_MOCK_TOOL_NAMES <= mcp.tools.keys()

    def test_registers_kiwoom_mock_us_tools(self) -> None:
        # ROB-867: KIWOOM profile registers both KR and US namespaces.
        mcp = _build_mcp(McpProfile.KIWOOM)
        assert KIWOOM_MOCK_US_TOOL_NAMES <= mcp.tools.keys()


class TestKiwoomKrProfile:
    """ROB-1159 — MCP_PROFILE=kiwoom_kr is MCP_PROFILE=kiwoom minus the whole
    kiwoom_mock_us_* namespace (4 mutations + 3 reads)."""

    def test_registers_exact_kr_kiwoom_namespace(self) -> None:
        mcp = _build_mcp(McpProfile.KIWOOM_KR)
        # Prefix-based, so a future kiwoom_mock_us_* (or any other new
        # kiwoom_mock_*) registration leaking into this profile fails here even
        # if the frozen name sets are also updated.
        registered = {name for name in mcp.tools if name.startswith("kiwoom_mock")}
        assert registered == KIWOOM_KR_TOOL_NAMES
        assert KIWOOM_KR_TOOL_NAMES == KIWOOM_MOCK_TOOL_NAMES

    def test_us_mutation_tools_are_absent(self) -> None:
        mcp = _build_mcp(McpProfile.KIWOOM_KR)
        assert KIWOOM_KR_EXCLUDED_US_MUTATION_TOOL_NAMES == {
            "kiwoom_mock_us_preview_order",
            "kiwoom_mock_us_place_order",
            "kiwoom_mock_us_modify_order",
            "kiwoom_mock_us_cancel_order",
        }
        leaked = KIWOOM_KR_EXCLUDED_US_MUTATION_TOOL_NAMES & mcp.tools.keys()
        assert not leaked, f"kiwoom_kr leaked US mutation tools: {sorted(leaked)}"

    def test_whole_us_namespace_is_absent(self) -> None:
        mcp = _build_mcp(McpProfile.KIWOOM_KR)
        assert KIWOOM_MOCK_US_TOOL_NAMES.isdisjoint(mcp.tools.keys())

    def test_matches_closed_world_and_known_mutation_contract(self) -> None:
        from app.mcp_server.tooling.route_request_lanes import (
            DIRECT_BROKER_MUTATION_TOOLS,
        )

        mcp = _build_mcp(McpProfile.KIWOOM_KR)
        assert set(mcp.tools) == kiwoom_kr_profile_tool_names()
        allowed_direct = KIWOOM_KR_TOOL_NAMES & DIRECT_BROKER_MUTATION_TOOLS
        assert mcp.tools.keys() & DIRECT_BROKER_MUTATION_TOOLS == allowed_direct

    def test_keeps_kt00007_order_detail_read(self) -> None:
        # ROB-1155's kiwoom_mock_get_order_detail is the reason this profile
        # exists: it is registered on kiwoom-family profiles only, and KR-B1
        # should reach it without also getting the US mutation surface.
        mcp = _build_mcp(McpProfile.KIWOOM_KR)
        assert "kiwoom_mock_get_order_detail" in mcp.tools

    def test_keeps_kr_order_surface_intact(self) -> None:
        from app.mcp_server.tooling.mirror_counterfactual_registration import (
            MIRROR_COUNTERFACTUAL_TOOL_NAMES,
        )

        # The split narrows registration only; the KR place/cancel/modify
        # surface a KR session needs stays present and identical to KIWOOM's.
        kiwoom = _build_mcp(McpProfile.KIWOOM)
        kiwoom_kr = _build_mcp(McpProfile.KIWOOM_KR)
        kr_names = {
            "kiwoom_mock_preview_order",
            "kiwoom_mock_place_order",
            "kiwoom_mock_cancel_order",
            "kiwoom_mock_modify_order",
        }
        assert kr_names <= kiwoom_kr.tools.keys()
        assert KIWOOM_MOCK_TOOL_NAMES <= kiwoom_kr.tools.keys()
        # ROB-1173 also removes the shared KIS mock mirror broker mutation.
        assert kiwoom.tools.keys() - kiwoom_kr.tools.keys() == (
            KIWOOM_MOCK_US_TOOL_NAMES | MIRROR_COUNTERFACTUAL_TOOL_NAMES
        )
        assert kiwoom_kr.tools.keys() - kiwoom.tools.keys() == set()

    def test_kiwoom_profile_still_registers_us_namespace(self) -> None:
        # The split is additive: MCP_PROFILE=kiwoom is deliberately unchanged by
        # ROB-1159, so a regression there is a separate, explicit decision.
        mcp = _build_mcp(McpProfile.KIWOOM)
        assert KIWOOM_MOCK_US_TOOL_NAMES <= mcp.tools.keys()


class TestKiwoomUsDefaultProfileGate:
    def test_registers_us_namespace_when_enabled(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "kiwoom_mock_us_enabled", True)
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert KIWOOM_MOCK_US_TOOL_NAMES <= mcp.tools.keys()

    def test_omits_us_namespace_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "kiwoom_mock_us_enabled", False)
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert KIWOOM_MOCK_US_TOOL_NAMES.isdisjoint(mcp.tools.keys())


class TestKiwoomDefaultProfileGate:
    """ROB-601: kiwoom_mock_* tools surface in the operator DEFAULT profile when
    ``settings.kiwoom_mock_enabled`` is true, so analyze→approval→order can run
    through kiwoom mock in the everyday session (the isolated KIWOOM profile
    drops every other broker's order surface and cannot substitute it).

    The flag defaults to ``False`` so the out-of-box DEFAULT surface is
    unchanged (pinned by ``TestOrderSurfaceMatrix``); fail-closed runtime config
    validation still blocks any tool call without real credentials.
    """

    def test_registers_kiwoom_mock_in_default_when_flag_enabled(
        self, monkeypatch
    ) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "kiwoom_mock_enabled", True)
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert KIWOOM_MOCK_TOOL_NAMES <= mcp.tools.keys()

    def test_omits_kiwoom_mock_in_default_when_flag_disabled(self, monkeypatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "kiwoom_mock_enabled", False)
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert KIWOOM_MOCK_TOOL_NAMES.isdisjoint(mcp.tools.keys())


_ALPACA_MUTATING = ALPACA_PAPER_MUTATING_TOOL_NAMES
_ORDER_SURFACE_MATRIX: dict[McpProfile, set[str]] = {
    McpProfile.DEFAULT: (
        _LEGACY_ORDER_TOOL_NAMES
        | KIS_LIVE_ORDER_TOOL_NAMES
        | KIS_MOCK_ORDER_TOOL_NAMES
        | LIVE_RECONCILE_TOOL_NAMES
        | TOSS_LIVE_ORDER_TOOL_NAMES
        | PAPER_LIMIT_ORDER_TOOL_NAMES
    ),
    McpProfile.HERMES_PAPER_KIS: set(KIS_MOCK_ORDER_TOOL_NAMES),
    McpProfile.CRYPTO: _LEGACY_ORDER_TOOL_NAMES | LIVE_RECONCILE_TOOL_NAMES,
    McpProfile.US_PAPER: set(_ALPACA_MUTATING) | ALPACA_PAPER_AUTOMATED_TOOL_NAMES,
    McpProfile.DB_PAPER: set(),
    McpProfile.KIWOOM: KIWOOM_MOCK_TOOL_NAMES | KIWOOM_MOCK_US_TOOL_NAMES,
    # ROB-1159 — KR-only split: the US namespace is physically absent.
    McpProfile.KIWOOM_KR: set(KIWOOM_MOCK_TOOL_NAMES),
    # ROB-697 M1 — shadow-replay registers zero order/mutation tools by design
    # (frozen-context read + policy + route_request only, early-return).
    McpProfile.SHADOW_REPLAY: set(),
    # Default-off profile: the direct registry exposes zero tools until the
    # dedicated feature flag is explicitly enabled.
    McpProfile.ALPACA_PAPER_CLEAN: set(),
    # ROB-1286 — the watch-fire repricing session. It may create an order
    # *proposal*; it holds no order-mutation tool at all, so this row is the
    # empty set and any future addition here is a visible diff.
    McpProfile.WATCH_REPRICING: set(),
    # FILL_WATCH_CONTEXT is a closed-world, context-only consumer profile.
    McpProfile.FILL_WATCH_CONTEXT: set(),
    # #1171 — the H3-CRYPTO paper pilot's only order surface is the ROB-703
    # paper simulator (no broker call); every live order tool is absent.
    McpProfile.H3_CRYPTO_PAPER: set(PAPER_LIMIT_ORDER_TOOL_NAMES),
    # #1257 — the H3-US paper pilot's only order surface is the manual Alpaca
    # paper submit/cancel pair (paper endpoint, account_mode alpaca_paper);
    # no reconcile writer, no automated submit, every live order tool absent.
    McpProfile.H3_US_PAPER: {"alpaca_paper_submit_order", "alpaca_paper_cancel_order"},
}
_ALL_ORDER_TOOL_NAMES = (
    _LEGACY_ORDER_TOOL_NAMES
    | KIS_LIVE_ORDER_TOOL_NAMES
    | KIS_MOCK_ORDER_TOOL_NAMES
    | LIVE_RECONCILE_TOOL_NAMES
    | KIWOOM_MOCK_TOOL_NAMES
    | KIWOOM_MOCK_US_TOOL_NAMES
    | _ALPACA_MUTATING
    | ALPACA_PAPER_AUTOMATED_TOOL_NAMES
    | TOSS_LIVE_ORDER_TOOL_NAMES
    | PAPER_LIMIT_ORDER_TOOL_NAMES
    | PAPER_EXECUTION_TOOL_NAMES
)


class TestOrderSurfaceMatrix:
    """Pin the exact order/mutation surface per profile (ROB-488).

    Catches both accidental additions (e.g. live tools leaking into a paper
    profile) and accidental removals (e.g. crypto losing its trading entry
    point) — set equality, not just subset.
    """

    @pytest.mark.parametrize("profile", list(McpProfile))
    def test_order_surface_matches_matrix(self, profile: McpProfile) -> None:
        mcp = _build_mcp(profile)
        registered_order_tools = _ALL_ORDER_TOOL_NAMES & mcp.tools.keys()
        assert registered_order_tools == _ORDER_SURFACE_MATRIX[profile], (
            f"profile={profile.value} order surface drifted: "
            f"extra={sorted(registered_order_tools - _ORDER_SURFACE_MATRIX[profile])}, "
            f"missing={sorted(_ORDER_SURFACE_MATRIX[profile] - registered_order_tools)}"
        )


# ROB-697 M1 — shadow-replay is a deliberate exception to the "every profile
# has the full read-only research surface" invariant below: it early-returns
# before the "Always" research-registration block, so it carries NO live-fetch
# tool (crypto research included). ``TestShadowReplayIsResearchSurfaceException``
# pins that omission explicitly.
_PROFILES_WITH_RESEARCH_SURFACE = [
    p
    for p in McpProfile
    if p
    not in (
        McpProfile.SHADOW_REPLAY,
        McpProfile.ALPACA_PAPER_CLEAN,
        # ROB-1286 — allowlist-only and early-returns before the "Always"
        # research block, like the other closed-world profiles above.
        McpProfile.WATCH_REPRICING,
        # FILL_WATCH_CONTEXT is its own two-tool, context-only closed world.
        McpProfile.FILL_WATCH_CONTEXT,
        # #1171 — the H3-CRYPTO paper surface is the runner's exact 20 tools.
        McpProfile.H3_CRYPTO_PAPER,
        # #1257 — the H3-US paper surface is the runner's exact 20 tools.
        McpProfile.H3_US_PAPER,
    )
]


class TestCryptoResearchToolsAllProfiles:
    """ROB-503: crypto read-only research tools register on EVERY profile
    that reaches the "Always" research block.

    ROB-488 had gated them to MCP_PROFILE=crypto, which broke single-server
    operation (crypto live trading runs on the DEFAULT server). Read-only
    tools carry no order-surface risk, so profile isolation buys nothing —
    except for shadow-replay (ROB-697), whose validity guard is that it
    carries NO live-fetch tool at all; see _PROFILES_WITH_RESEARCH_SURFACE.
    """

    @pytest.mark.parametrize("profile", _PROFILES_WITH_RESEARCH_SURFACE)
    def test_crypto_research_tools_registered(self, profile: McpProfile) -> None:
        mcp = _build_mcp(profile)
        missing = _CRYPTO_RESEARCH_TOOL_NAMES - mcp.tools.keys()
        assert not missing, f"profile={profile.value} missing: {sorted(missing)}"

    @pytest.mark.parametrize("profile", list(McpProfile))
    def test_removed_generic_names_absent(self, profile: McpProfile) -> None:
        mcp = _build_mcp(profile)
        leaked = _REMOVED_GENERIC_TOOL_NAMES & mcp.tools.keys()
        assert not leaked, f"profile={profile.value} leaked old names: {sorted(leaked)}"


class TestShadowReplayIsResearchSurfaceException:
    """ROB-697 M1 — pin that shadow-replay is the ONE profile without the
    default research surface (the load-bearing validity guard: a headless
    replay must not be able to reach live market data)."""

    def test_shadow_replay_has_no_crypto_research_tools(self) -> None:
        mcp = _build_mcp(McpProfile.SHADOW_REPLAY)
        assert _CRYPTO_RESEARCH_TOOL_NAMES.isdisjoint(mcp.tools.keys())


class TestRouteProposalReadinessByProfile:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("profile", [McpProfile.DEFAULT])
    async def test_proposal_gate_on_is_ready(
        self,
        monkeypatch: pytest.MonkeyPatch,
        profile: McpProfile,
    ) -> None:
        monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
        mcp = _build_mcp(profile)

        result = await mcp.tools["route_request"](
            intent="buy_analysis",
            market="kr",
        )

        assert "order_proposal_create" in mcp.tools
        assert result["success"] is True
        assert result["route_contract"]["execution_ready"] is True
        assert result["standard_tool_sequence"][-1]["tool"] == ("order_proposal_create")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("profile", [McpProfile.DEFAULT])
    async def test_proposal_gate_off_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        profile: McpProfile,
    ) -> None:
        monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", False)
        mcp = _build_mcp(profile)

        result = await mcp.tools["route_request"](
            intent="profit_taking",
            market="kr",
        )

        assert "order_proposal_create" not in mcp.tools
        assert result["success"] is False
        assert result["error"] == "required_route_tool_unavailable"
        assert result["route_contract"]["execution_ready"] is False


class TestResolveMcpProfile:
    def test_none_returns_default(self) -> None:
        assert resolve_mcp_profile(None) is McpProfile.DEFAULT

    def test_empty_string_returns_default(self) -> None:
        assert resolve_mcp_profile("") is McpProfile.DEFAULT

    def test_whitespace_only_returns_default(self) -> None:
        assert resolve_mcp_profile("   ") is McpProfile.DEFAULT

    def test_explicit_default(self) -> None:
        assert resolve_mcp_profile("default") is McpProfile.DEFAULT

    def test_hermes_paper_kis(self) -> None:
        assert resolve_mcp_profile("hermes-paper-kis") is McpProfile.HERMES_PAPER_KIS

    def test_us_paper(self) -> None:
        assert resolve_mcp_profile("us-paper") is McpProfile.US_PAPER

    def test_db_paper(self) -> None:
        assert resolve_mcp_profile("db-paper") is McpProfile.DB_PAPER

    def test_crypto(self) -> None:
        assert resolve_mcp_profile("crypto") is McpProfile.CRYPTO

    def test_kiwoom(self) -> None:
        assert resolve_mcp_profile("kiwoom") is McpProfile.KIWOOM

    def test_shadow_replay(self) -> None:
        assert resolve_mcp_profile("shadow-replay") is McpProfile.SHADOW_REPLAY

    @pytest.mark.parametrize(
        "retired",
        [
            "analysis_readonly",
            "account_read",
            "tradingcodex_execution",
            "live-kr",
            "live-us",
            "live-crypto",
        ],
    )
    def test_profiles_retired_by_rob1436_are_rejected(self, retired: str) -> None:
        with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
            resolve_mcp_profile(retired)

    def test_removed_paper_execution_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
            resolve_mcp_profile("paper_execution")

    def test_invalid_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
            resolve_mcp_profile("unknown-profile")

    def test_invalid_string_mentions_allowed_values(self) -> None:
        with pytest.raises(ValueError, match="default"):
            resolve_mcp_profile("bogus")
