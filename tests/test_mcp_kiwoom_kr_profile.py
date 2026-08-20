"""ROB-1159 — KR-only Kiwoom MCP profile registration guards.

``tests/test_mcp_profiles.py`` pins the profile→tool-surface contract. This file
pins the *mechanism*: that ``register_kiwoom_kr_tools`` filters at registration
time rather than merely reflecting what ``orders_kiwoom_variants`` happens to
register today, and that the KR order path is untouched by the split.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.core.config import settings
from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling import kiwoom_kr_registration, registry
from app.mcp_server.tooling.analysis_bundle_handlers import ANALYSIS_BUNDLE_TOOL_NAMES
from app.mcp_server.tooling.investment_hermes_handlers import (
    INVESTMENT_HERMES_TOOL_NAMES,
)
from app.mcp_server.tooling.investment_snapshots_registration import (
    INVESTMENT_SNAPSHOTS_TOOL_NAMES,
)
from app.mcp_server.tooling.kiwoom_kr_registration import (
    KIWOOM_KR_BASE_PROFILE_TOOL_NAMES,
    KIWOOM_KR_EXCLUDED_US_MUTATION_TOOL_NAMES,
    KIWOOM_KR_TOOL_NAMES,
    kiwoom_kr_profile_tool_names,
    register_kiwoom_kr_tools,
)
from app.mcp_server.tooling.order_proposal_tools import ORDER_PROPOSAL_TOOL_NAMES
from app.mcp_server.tooling.orders_kiwoom_us_variants import (
    KIWOOM_MOCK_US_MUTATION_TOOL_NAMES,
    KIWOOM_MOCK_US_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from tests._mcp_tooling_support import DummyMCP

_EXPECTED_KR_TOOL_NAMES = {
    "kiwoom_mock_preview_order",
    "kiwoom_mock_place_order",
    "kiwoom_mock_cancel_order",
    "kiwoom_mock_modify_order",
    "kiwoom_mock_get_order_history",
    "kiwoom_mock_get_order_detail",
    "kiwoom_mock_get_positions",
    "kiwoom_mock_get_orderable_cash",
}
_EXPECTED_US_MUTATION_TOOL_NAMES = {
    "kiwoom_mock_us_preview_order",
    "kiwoom_mock_us_place_order",
    "kiwoom_mock_us_modify_order",
    "kiwoom_mock_us_cancel_order",
}


class TestProfileResolution:
    def test_kiwoom_kr_is_a_resolvable_profile(self) -> None:
        assert resolve_mcp_profile("kiwoom_kr") is McpProfile.KIWOOM_KR

    def test_kiwoom_kr_is_distinct_from_kiwoom(self) -> None:
        assert McpProfile.KIWOOM_KR is not McpProfile.KIWOOM
        assert McpProfile.KIWOOM_KR.value == "kiwoom_kr"


class TestKiwoomKrNameSets:
    def test_kr_allowlist_is_exactly_the_eight_kr_tools(self) -> None:
        assert KIWOOM_KR_TOOL_NAMES == _EXPECTED_KR_TOOL_NAMES
        assert len(KIWOOM_KR_TOOL_NAMES) == 8
        assert KIWOOM_KR_TOOL_NAMES == KIWOOM_MOCK_TOOL_NAMES

    def test_excluded_us_mutations_are_exactly_the_four(self) -> None:
        assert KIWOOM_KR_EXCLUDED_US_MUTATION_TOOL_NAMES == (
            _EXPECTED_US_MUTATION_TOOL_NAMES
        )
        assert KIWOOM_KR_EXCLUDED_US_MUTATION_TOOL_NAMES == (
            KIWOOM_MOCK_US_MUTATION_TOOL_NAMES
        )

    def test_allowlist_is_disjoint_from_the_us_namespace(self) -> None:
        assert KIWOOM_KR_TOOL_NAMES.isdisjoint(KIWOOM_MOCK_US_TOOL_NAMES)


class TestRegistrarRegistersOnlyKrTools:
    def test_registers_exactly_the_kr_allowlist(self) -> None:
        mcp = DummyMCP()
        register_kiwoom_kr_tools(cast(Any, mcp))
        assert set(mcp.tools) == KIWOOM_KR_TOOL_NAMES

    def test_registers_no_us_tool(self) -> None:
        mcp = DummyMCP()
        register_kiwoom_kr_tools(cast(Any, mcp))
        assert KIWOOM_MOCK_US_TOOL_NAMES.isdisjoint(set(mcp.tools))
        assert not [name for name in mcp.tools if name.startswith("kiwoom_mock_us_")]

    def test_allowlist_filter_is_load_bearing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unlisted tool added inside the KR registrar must be dropped.

        Without the ``_AllowlistedMCP`` proxy this test fails: the stub
        registrar below registers a KR tool *and* a US mutation tool, exactly
        the drift this profile must survive.
        """

        def _stub_register(mcp: Any) -> None:
            @mcp.tool(
                name="kiwoom_mock_place_order",
                description="stub KR tool",
            )
            def _kr_tool() -> None:  # pragma: no cover - registration only
                return None

            @mcp.tool(
                name="kiwoom_mock_us_place_order",
                description="stub US mutation tool",
            )
            def _us_tool() -> None:  # pragma: no cover - registration only
                return None

        monkeypatch.setattr(
            kiwoom_kr_registration, "register_kiwoom_mock_tools", _stub_register
        )
        mcp = DummyMCP()
        register_kiwoom_kr_tools(cast(Any, mcp))

        assert "kiwoom_mock_place_order" in mcp.tools
        assert "kiwoom_mock_us_place_order" not in mcp.tools


class TestWholeProfileClosedWorld:
    def test_base_inventory_includes_readonly_advisors_and_is_exactly_122_tools(
        self,
    ) -> None:
        # Closed world: the count moves only with a reviewed addition. ROB-1303
        # added get_spike_attribution (read-only attribution reader), which the
        # KIWOOM <-> KIWOOM_KR shared-surface contract requires here — see
        # TestKiwoomKrProfile::test_keeps_kr_order_surface_intact. ROB-1309
        # added screen_stocks_enrich alongside screen_stocks_snapshot.
        assert "evaluate_buy_gate_ab_shadow" in KIWOOM_KR_BASE_PROFILE_TOOL_NAMES
        assert "get_spike_attribution" in KIWOOM_KR_BASE_PROFILE_TOOL_NAMES
        assert "screen_stocks_enrich" in KIWOOM_KR_BASE_PROFILE_TOOL_NAMES
        assert len(KIWOOM_KR_BASE_PROFILE_TOOL_NAMES) == 122

    def test_current_profile_matches_active_exact_set(self) -> None:
        mcp = DummyMCP()
        registry.register_all_tools(cast(Any, mcp), profile=McpProfile.KIWOOM_KR)
        assert set(mcp.tools) == kiwoom_kr_profile_tool_names()

    def test_unclassified_shared_broker_alias_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shared registrar cannot widen KR outside the profile exact set."""
        original_register = registry.register_market_data_tools

        def _register_with_shadow_alias(mcp: Any) -> None:
            original_register(mcp)

            @mcp.tool(
                name="kis_mock_shadow_place_order",
                description="unclassified verifier mutation",
            )
            def _shadow_tool() -> None:  # pragma: no cover - registration only
                return None

        monkeypatch.setattr(
            registry,
            "register_market_data_tools",
            _register_with_shadow_alias,
        )
        mcp = DummyMCP()
        registry.register_all_tools(cast(Any, mcp), profile=McpProfile.KIWOOM_KR)

        assert "kis_mock_shadow_place_order" not in mcp.tools
        assert set(mcp.tools) == kiwoom_kr_profile_tool_names()

    @pytest.mark.parametrize(
        ("enabled_setting", "expected_optional_names"),
        [
            (
                "ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED",
                ANALYSIS_BUNDLE_TOOL_NAMES,
            ),
            (
                "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED",
                INVESTMENT_HERMES_TOOL_NAMES
                | {"investment_report_generate_from_bundle"},
            ),
            (
                "INVESTMENT_SNAPSHOTS_MCP_ENABLED",
                INVESTMENT_SNAPSHOTS_TOOL_NAMES,
            ),
            ("ORDER_PROPOSALS_ENABLED", ORDER_PROPOSAL_TOOL_NAMES),
        ],
    )
    def test_optional_gates_expand_only_their_reviewed_exact_sets(
        self,
        monkeypatch: pytest.MonkeyPatch,
        enabled_setting: str,
        expected_optional_names: set[str],
    ) -> None:
        optional_settings = {
            "ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED",
            "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED",
            "INVESTMENT_SNAPSHOTS_MCP_ENABLED",
            "ORDER_PROPOSALS_ENABLED",
        }
        for setting_name in optional_settings:
            monkeypatch.setattr(settings, setting_name, setting_name == enabled_setting)

        mcp = DummyMCP()
        registry.register_all_tools(cast(Any, mcp), profile=McpProfile.KIWOOM_KR)

        assert set(mcp.tools) == (
            set(KIWOOM_KR_BASE_PROFILE_TOOL_NAMES) | expected_optional_names
        )


class TestKrOrderPathUnchanged:
    """🔴 ROB-1159 touches profile registration only. These pin the KR order
    invariants the 2026-07-30 KR-B1 P0 session depends on."""

    def test_domestic_exchange_stays_krx_pinned(self) -> None:
        from app.services.brokers.kiwoom import constants

        assert constants.MOCK_EXCHANGE_KRX == "KRX"
        assert set(constants.MOCK_REJECTED_EXCHANGES) == {"NXT", "SOR"}

    @pytest.mark.asyncio
    async def test_place_order_body_still_pins_krx(self) -> None:
        from app.services.brokers.kiwoom.domestic_orders import (
            KiwoomDomesticOrderClient,
        )

        sent: dict[str, Any] = {}

        class _RecordingClient:
            async def post_api(
                self,
                api_id: str,
                path: str,
                body: dict[str, Any],
                cont_yn: str | None = None,
                next_key: str | None = None,
            ) -> dict[str, Any]:
                sent.update(body)
                return {"return_code": 0}

        client = KiwoomDomesticOrderClient(cast(Any, _RecordingClient()))
        await client.place_buy_order(symbol="005930", quantity=1, price=70000)

        assert sent["dmst_stex_tp"] == "KRX"

    def test_registered_kr_tools_are_the_shared_registrar_callables(self) -> None:
        # The allowlist proxy must pass the *same* callables through, not wrap or
        # re-implement them: kiwoom_kr and kiwoom register identical KR tools.
        from app.mcp_server.tooling import orders_kiwoom_variants

        direct = DummyMCP()
        orders_kiwoom_variants.register(cast(Any, direct))
        filtered = DummyMCP()
        register_kiwoom_kr_tools(cast(Any, filtered))

        assert set(filtered.tools) == set(direct.tools) & KIWOOM_KR_TOOL_NAMES
        for name in KIWOOM_KR_TOOL_NAMES:
            # Same code object => same tool implementation. (Each register()
            # call builds fresh closures, so identity would not hold.)
            assert filtered.tools[name].__code__ is direct.tools[name].__code__
