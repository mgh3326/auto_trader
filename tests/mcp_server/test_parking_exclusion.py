"""#883 cash-sweep ``parking_exclusion`` — typed read + fail-closed parsing.

The ``get_parking_exclusion`` MCP tool may only ever read
``user_settings.parking_exclusion`` for the MCP user. A malformed stored value
must surface ``status="unknown"`` with ``exclusions=None`` — never a zero
exclusion — so the sweep playbook parks nothing on unparseable input.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.mcp_server.tooling import user_settings_tools
from app.mcp_server.tooling.route_request_lanes import (
    MUTATION_TOOLS,
    READ_ONLY_ADVISORY_TOOLS,
)
from app.mcp_server.tooling.user_settings_tools import (
    get_parking_exclusion,
    set_user_setting,
)
from app.services.parking_exclusion_settings import (
    PARKING_EXCLUSION_CURRENCIES,
    PARKING_EXCLUSION_KEY,
    ParkingExclusionValidationError,
    normalize_generic_parking_exclusion_write,
    parse_parking_exclusion_value,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_DIR = REPO_ROOT / "config" / "mcp_lane_allowlists"
SPEC_BASIS = "spec:883-cash-sweep"
_SWEEP_LANES = ("kr", "us")
_TS = datetime(2026, 9, 28, 9, 0, 0, tzinfo=UTC)


def _session_cm(session: AsyncMock | MagicMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm


def _patch_session_returning(row: Any) -> Any:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: row)
    )
    return patch(
        "app.mcp_server.tooling.user_settings_tools._session_factory",
        return_value=MagicMock(return_value=_session_cm(session)),
    )


def _lane_rows(lane: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in (ALLOWLIST_DIR / f"{lane}.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            tool, basis = line.split("\t")
            rows.append((tool, basis))
    return rows


class TestParseParkingExclusionValue:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ({}, {}),
            ({"KRW": 500000}, {"KRW": 500000}),
            ({"KRW": 0, "USD": 0}, {"KRW": 0, "USD": 0}),
            ({"KRW": "500000", "USD": "100.50"}, {"KRW": 500000, "USD": "100.50"}),
            ({"KRW": 500000.0}, {"KRW": 500000}),
        ],
    )
    def test_valid_values_parse(self, value: Any, expected: dict[str, Any]) -> None:
        parsed = parse_parking_exclusion_value(value)
        assert parsed is not None
        assert parsed == {k: Decimal(str(v)) for k, v in expected.items()}

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "500000",
            500000,
            0,
            True,
            ["KRW", 500000],
            {"KRW": None},
            {"KRW": True},
            {"KRW": "garbage"},
            {"KRW": "NaN"},
            {"KRW": "Infinity"},
            {"KRW": float("inf")},
            {"KRW": -1},
            {"KRW": {"amount": 5}},
            {"JPY": 5},
            {"krw": 5},
            {"KRW": 5, "JPY": 1},
            {"KRW": 5, "USD": "garbage"},
            {"KRW": 5, "source": "operator_confirmed"},
        ],
    )
    def test_any_malformation_poisons_the_whole_value(self, value: Any) -> None:
        """A single bad key or amount must not leave a partially-applied map."""
        assert parse_parking_exclusion_value(value) is None


class TestGetParkingExclusion:
    @pytest.mark.asyncio
    async def test_absent_row_defaults_every_currency_to_zero(self) -> None:
        with _patch_session_returning(None):
            result = await get_parking_exclusion()

        assert result == {
            "success": True,
            "status": "ok",
            "exclusions": {"KRW": "0", "USD": "0"},
            "reason": None,
            "updated_at": None,
        }

    @pytest.mark.asyncio
    async def test_per_currency_amounts_round_trip_as_decimal_strings(self) -> None:
        row = SimpleNamespace(value={"KRW": 500000, "USD": "100.50"}, updated_at=_TS)
        with _patch_session_returning(row):
            result = await get_parking_exclusion()

        assert result == {
            "success": True,
            "status": "ok",
            "exclusions": {"KRW": "500000", "USD": "100.50"},
            "reason": None,
            "updated_at": _TS.isoformat(),
        }

    @pytest.mark.asyncio
    async def test_currency_absent_from_a_valid_value_reports_zero(self) -> None:
        row = SimpleNamespace(value={"USD": 25}, updated_at=_TS)
        with _patch_session_returning(row):
            result = await get_parking_exclusion()

        assert result["status"] == "ok"
        assert result["exclusions"] == {"KRW": "0", "USD": "25"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value",
        [
            "500000",
            500000,
            ["KRW"],
            {"KRW": None},
            {"KRW": True},
            {"KRW": "garbage"},
            {"KRW": "NaN"},
            {"KRW": float("inf")},
            {"KRW": -5},
            {"KRW": {"amount": 5}},
            {"JPY": 5},
            {"KRW": 5, "JPY": 1},
        ],
    )
    async def test_malformed_value_fails_closed_to_unknown(self, value: Any) -> None:
        """Assertion-RED target: a mutant reading malformed input as zero
        exclusion turns every one of these RED."""
        row = SimpleNamespace(value=value, updated_at=_TS)
        with _patch_session_returning(row):
            result = await get_parking_exclusion()

        assert result["status"] == "unknown"
        assert result["exclusions"] is None
        assert result["reason"] == "malformed_value"
        # unknown is never a zero exclusion — the playbook parks nothing
        assert result["exclusions"] != dict.fromkeys(PARKING_EXCLUSION_CURRENCIES, "0")
        assert result["success"] is True
        assert result["updated_at"] == _TS.isoformat()

    @pytest.mark.asyncio
    async def test_read_failure_is_unknown_never_zero(self) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        with patch(
            "app.mcp_server.tooling.user_settings_tools._session_factory",
            return_value=MagicMock(return_value=_session_cm(session)),
        ):
            result = await get_parking_exclusion()

        assert result == {
            "success": False,
            "status": "unknown",
            "exclusions": None,
            "reason": "read_failed",
            "updated_at": None,
        }

    @pytest.mark.asyncio
    async def test_only_the_parking_exclusion_key_is_ever_read(self) -> None:
        """No other user_settings key can be read through this tool."""
        seen_keys: list[str] = []

        async def _spy(key: str) -> None:
            seen_keys.append(key)
            return None

        with patch.object(user_settings_tools, "_get_setting_row", side_effect=_spy):
            await get_parking_exclusion()

        assert seen_keys == [PARKING_EXCLUSION_KEY]
        # and there is no key parameter a caller could use to reach others
        assert list(inspect.signature(get_parking_exclusion).parameters) == []


class TestParkingExclusionWritePath:
    def test_normalize_canonicalizes_amounts_to_decimal_strings(self) -> None:
        assert normalize_generic_parking_exclusion_write(
            {"KRW": 500000, "USD": 100.5}
        ) == {"KRW": "500000", "USD": "100.5"}
        assert normalize_generic_parking_exclusion_write({}) == {}

    @pytest.mark.parametrize(
        "value",
        [
            "500000",
            {"KRW": -1},
            {"KRW": "NaN"},
            {"JPY": 5},
            {"KRW": 5, "source": "operator_confirmed"},
        ],
    )
    def test_normalize_rejects_malformed_writes(self, value: Any) -> None:
        with pytest.raises(ParkingExclusionValidationError):
            normalize_generic_parking_exclusion_write(value)

    @pytest.mark.asyncio
    async def test_set_user_setting_rejects_malformed_before_db(self) -> None:
        factory = MagicMock()
        with patch(
            "app.mcp_server.tooling.user_settings_tools._session_factory",
            return_value=factory,
        ):
            with pytest.raises(ParkingExclusionValidationError):
                await set_user_setting(key=PARKING_EXCLUSION_KEY, value={"KRW": -1})
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_user_setting_stores_canonical_parking_exclusion(self) -> None:
        session = MagicMock()
        row = MagicMock()
        row.key = PARKING_EXCLUSION_KEY
        row.value = {"KRW": "500000"}
        row.updated_at = _TS
        session.execute = AsyncMock(
            side_effect=[
                SimpleNamespace(),
                SimpleNamespace(scalar_one=lambda: row),
            ]
        )
        session.commit = AsyncMock()
        tx_cm = AsyncMock()
        tx_cm.__aenter__.return_value = None
        tx_cm.__aexit__.return_value = None
        session.begin = MagicMock(return_value=tx_cm)
        with patch(
            "app.mcp_server.tooling.user_settings_tools._session_factory",
            return_value=MagicMock(return_value=_session_cm(session)),
        ):
            result = await set_user_setting(
                key=PARKING_EXCLUSION_KEY, value={"KRW": 500000}
            )

        upsert_stmt = session.execute.await_args_list[0].args[0]
        params = upsert_stmt.compile(dialect=postgresql.dialect()).params
        assert {"KRW": "500000"} in params.values()
        assert result["value"] == {"KRW": "500000"}

    @pytest.mark.asyncio
    async def test_set_user_setting_other_keys_are_untouched(self) -> None:
        """The parking_exclusion guard must not leak onto other keys."""
        session = MagicMock()
        row = MagicMock()
        row.key = "some_other_key"
        row.value = {"anything": "goes"}
        row.updated_at = _TS
        session.execute = AsyncMock(
            side_effect=[
                SimpleNamespace(),
                SimpleNamespace(scalar_one=lambda: row),
            ]
        )
        session.commit = AsyncMock()
        tx_cm = AsyncMock()
        tx_cm.__aenter__.return_value = None
        tx_cm.__aexit__.return_value = None
        session.begin = MagicMock(return_value=tx_cm)
        with patch(
            "app.mcp_server.tooling.user_settings_tools._session_factory",
            return_value=MagicMock(return_value=_session_cm(session)),
        ):
            result = await set_user_setting(
                key="some_other_key", value={"anything": "goes"}
            )
        assert result["value"] == {"anything": "goes"}


class TestExposureBoundary:
    """Assertion-RED guards on the kr/us lane exposure surface."""

    def test_sweep_lanes_add_exactly_the_two_read_tools(self) -> None:
        for lane in _SWEEP_LANES:
            rows = _lane_rows(lane)
            added = {tool for tool, basis in rows if basis == SPEC_BASIS}
            assert added == {"toss_proposal_accounts", "get_parking_exclusion"}, (
                f"{lane}: spec:883-cash-sweep rows must be exactly the two "
                f"read-only sweep tools, got {sorted(added)}"
            )

    def test_added_tools_are_classified_read_only_not_mutation(self) -> None:
        for tool in ("toss_proposal_accounts", "get_parking_exclusion"):
            assert tool in READ_ONLY_ADVISORY_TOOLS
            assert tool not in MUTATION_TOOLS

    def test_generic_user_setting_tools_stay_off_sweep_lanes(self) -> None:
        """No generic user_settings read/write may ride the execution lanes."""
        for lane in _SWEEP_LANES:
            tools = {tool for tool, _basis in _lane_rows(lane)}
            assert "get_user_setting" not in tools
            assert "set_user_setting" not in tools

    def test_no_write_or_order_tool_is_added(self) -> None:
        """Mutant guard: any write/order tool under the 883 basis goes RED."""
        handler_writers = {
            "set_user_setting",
            "update_manual_holdings",
            "session_context_append",
            "analysis_artifact_save",
            "forecast_save",
            "forecast_resolve",
            "decision_table_apply",
        }
        for lane in _SWEEP_LANES:
            rows = _lane_rows(lane)
            added = {tool for tool, basis in rows if basis == SPEC_BASIS}
            assert not (added & MUTATION_TOOLS)
            assert not (added & handler_writers)

    def test_get_parking_exclusion_registers_on_default_only(self, monkeypatch) -> None:
        from tests.mcp_server._registration_recorder import collect_profile_tools

        profiles = collect_profile_tools(monkeypatch, gates_enabled=True)
        for profile, tools in profiles.items():
            if profile == "default":
                assert "get_parking_exclusion" in tools
            else:
                assert "get_parking_exclusion" not in tools, (
                    f"get_parking_exclusion leaked onto {profile}"
                )

        off = collect_profile_tools(monkeypatch, gates_enabled=False)
        assert "get_parking_exclusion" not in off["default"], (
            "ORDER_PROPOSALS_ENABLED=false must keep the read absent"
        )

    def test_user_settings_tool_names_pair_is_not_widened(self) -> None:
        from app.mcp_server.tooling.user_settings_registration import (
            PARKING_EXCLUSION_TOOL_NAMES,
            USER_SETTINGS_TOOL_NAMES,
        )

        assert USER_SETTINGS_TOOL_NAMES == {"get_user_setting", "set_user_setting"}
        assert PARKING_EXCLUSION_TOOL_NAMES == {"get_parking_exclusion"}
        assert USER_SETTINGS_TOOL_NAMES.isdisjoint(PARKING_EXCLUSION_TOOL_NAMES)
