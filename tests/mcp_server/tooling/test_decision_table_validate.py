"""Acceptance tests for the pure ROB-1348 decision-table validator."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import math
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.mcp_server.tooling.analysis_readonly_registration import (
    ANALYSIS_READONLY_TOOL_NAMES,
)
from app.mcp_server.tooling.analysis_registration import ANALYSIS_TOOL_NAMES
from app.mcp_server.tooling.decision_table_registration import (
    DECISION_TABLE_TOOL_NAMES,
    register_decision_table_tools,
)
from app.services.decision_table_validate import decision_table_validate
from app.services.trading_policy_service import policy_version_stamp

pytestmark = pytest.mark.unit

_FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "decision_table_validate"
_CURRENT_SCHEMA = "kr-nxt-decision-table/v1.1"
_DEPRECATED_SCHEMA = "kr-nxt-decision-table/v1"
_REAL_FIXTURES = (
    "kr-nxt-decision-table-2026-09-02.json",
    "kr-nxt-decision-table-2026-09-03.json",
    "kr-nxt-decision-table-2026-09-04.json",
)
_HAPPY_FIXTURE = "kr-nxt-decision-table-v11-happy-path.json"


def _canonical_hash(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _reported_v1_envelope(name: str) -> dict[str, Any]:
    fixture = _fixture(name)
    provenance = fixture["_fixture_provenance"]
    date = provenance["correlation_id"].removeprefix("kr-nxt-prep-")
    # Only values printed in the report headers are transcribed. The reports did
    # not print a payload snapshot, handoff queue, or compliance stamp, so none
    # is fabricated for these verbatim historical decision tables.
    return {
        "schema_version": _DEPRECATED_SCHEMA,
        "correlation_id": provenance["correlation_id"],
        "trading_date": date,
        "market": "kr",
        "valid_window": {
            "starts_at": f"{date}T08:00:00+09:00",
            "ends_at": f"{date}T08:50:00+09:00",
        },
        "decision_table": deepcopy(fixture["decision_table"]),
        "decision_table_hash": provenance["recorded_decision_table_hash"],
    }


def _happy_envelope() -> dict[str, Any]:
    fixture = _fixture(_HAPPY_FIXTURE)
    return deepcopy(fixture["envelope"])


def _rehash(envelope: dict[str, Any]) -> None:
    envelope["decision_table_hash"] = _canonical_hash(envelope["decision_table"])


def _rules(result: dict[str, Any]) -> set[str]:
    return {item["rule"] for item in result["violations"]}


def _violation(result: dict[str, Any], rule: str) -> dict[str, Any]:
    return next(item for item in result["violations"] if item["rule"] == rule)


def test_constructed_v11_happy_path_is_explicitly_not_a_real_artifact() -> None:
    fixture = _fixture(_HAPPY_FIXTURE)

    assert fixture["_fixture_provenance"]["kind"] == (
        "constructed_from_merged_prompt_skeleton"
    )
    assert fixture["_fixture_provenance"]["real_artifact_claim"] is False
    result = decision_table_validate(fixture["envelope"], "kr")

    assert result["valid"] is True
    assert result["detected_shape"] == "row_object_rungs_v11"
    assert result["violations"] == []
    assert result["recomputed"]["hash"] == fixture["envelope"]["decision_table_hash"]
    assert result["recomputed"]["rows"] == [
        {
            "scenario_id": "constructed-v11-196170-breakeven-reserve-trim",
            "price": 315000,
            "qty": 1,
        }
    ]
    assert result["policy"] == policy_version_stamp()


def test_real_fixture_09_04_parallel_rungs_is_blocked_under_v11() -> None:
    result = decision_table_validate(
        _reported_v1_envelope("kr-nxt-decision-table-2026-09-04.json"), "kr"
    )

    assert result["valid"] is False
    assert result["detected_shape"] == "row_object_parallel_list_rungs"
    assert {
        "schema_version_deprecated_v1",
        "unsupported_rungs_encoding",
    } <= _rules(result)
    assert _violation(result, "schema_version_deprecated_v1")["severity"] == "advisory"
    assert _violation(result, "schema_version_deprecated_v1")["expected"].endswith(
        "2026-09-12"
    )
    assert (
        result["recomputed"]["hash"]
        == "a9e0a80baf84d12147ba7ea31cd8d92fb30a55d58447723ffcd80da56e7dab7c"
    )


def test_real_fixture_09_03_prose_rungs_is_blocked_under_v11() -> None:
    result = decision_table_validate(
        _reported_v1_envelope("kr-nxt-decision-table-2026-09-03.json"), "kr"
    )

    assert result["valid"] is False
    assert result["detected_shape"] == "row_object_prose_rungs"
    assert {
        "schema_version_deprecated_v1",
        "price_qty_not_machine_recomputable",
    } <= _rules(result)


def test_real_fixture_09_02_scalar_rungs_is_blocked_under_v11() -> None:
    result = decision_table_validate(
        _reported_v1_envelope("kr-nxt-decision-table-2026-09-02.json"), "kr"
    )

    assert result["valid"] is False
    assert result["detected_shape"] == "row_object_scalar_rungs"
    assert {
        "schema_version_deprecated_v1",
        "unsupported_rungs_encoding",
    } <= _rules(result)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "kr-nxt-decision-table-2026-09-02.json",
            Counter(
                schema_version_deprecated_v1=1,
                unsupported_rungs_encoding=3,
            ),
        ),
        (
            "kr-nxt-decision-table-2026-09-03.json",
            Counter(
                schema_version_deprecated_v1=1,
                unknown_top_level_key=1,
                price_qty_not_machine_recomputable=5,
            ),
        ),
        (
            "kr-nxt-decision-table-2026-09-04.json",
            Counter(
                schema_version_deprecated_v1=1,
                unknown_top_level_key=4,
                unsupported_rungs_encoding=23,
            ),
        ),
    ],
)
def test_real_fixture_golden_violation_counts_are_observed(
    name: str, expected: Counter[str]
) -> None:
    result = decision_table_validate(_reported_v1_envelope(name), "kr")

    assert Counter(item["rule"] for item in result["violations"]) == expected


def test_real_fixture_provenance_marks_verbatim_table_and_transcribed_envelope() -> (
    None
):
    for name in _REAL_FIXTURES:
        provenance = _fixture(name)["_fixture_provenance"]
        assert provenance["decision_table"] == "verbatim"
        assert provenance["envelope"] == (
            "transcribed from reports/kr-nxt-prep-"
            f"{provenance['correlation_id'].removeprefix('kr-nxt-prep-')}.md"
        )


def test_columnar_shape_is_explicit_block() -> None:
    """09-04 saved columnar data is unavailable; detect only columns plus list rows."""
    envelope = _happy_envelope()
    envelope["decision_table"] = {"columns": ["scenario_id"], "rows": [["x"]]}
    _rehash(envelope)

    result = decision_table_validate(envelope, "kr")

    assert result["detected_shape"] == "columnar"
    assert result["valid"] is False
    assert "unsupported_table_shape" in _rules(result)


def test_m1_one_tick_price_mutant_blocks_recomputation() -> None:
    envelope = _reported_v1_envelope("kr-nxt-decision-table-2026-09-04.json")
    rungs = envelope["decision_table"]["rows"][0]["action"]["rungs"]
    rungs["prices"][0] += 500
    _rehash(envelope)

    result = decision_table_validate(envelope, "kr")

    assert "price_recompute_mismatch" in _rules(result)
    assert _violation(result, "price_recompute_mismatch")["severity"] == "block"


def test_m2_one_byte_hash_mutant_blocks() -> None:
    envelope = _happy_envelope()
    envelope["decision_table_hash"] = "b" + envelope["decision_table_hash"][1:]

    result = decision_table_validate(envelope, "kr")

    assert result["valid"] is False
    assert "decision_table_hash_mismatch" in _rules(result)


def test_m3_sell_below_avg_times_101_blocks_loss_guard() -> None:
    envelope = _happy_envelope()
    rung = envelope["decision_table"]["rows"][0]["action"]["rungs"][0]
    rung["price_min"] = 312000
    rung["price_max"] = 312000
    _rehash(envelope)

    result = decision_table_validate(envelope, "kr")

    assert result["valid"] is False
    assert "loss_guard_violation" in _rules(result)


def test_m4_opposite_order_rows_block_same_day_chain() -> None:
    envelope = _happy_envelope()
    opposite = deepcopy(envelope["decision_table"]["rows"][0])
    opposite["scenario_id"] = "constructed-v11-196170-opposite"
    opposite["action"]["side"] = "buy"
    envelope["decision_table"]["rows"].append(opposite)
    _rehash(envelope)

    result = decision_table_validate(envelope, "kr")

    assert result["valid"] is False
    assert "same_day_chain_or_opposite_order" in _rules(result)


def test_schema_nonfinite_hash_parent_and_market_contracts() -> None:
    envelope = _happy_envelope()
    envelope["schema_version"] = "wrong"
    envelope["parent_correlation_id"] = "not-a-prep-correlation"
    envelope["decision_table"]["rows"][0]["conditions"][0]["value"] = math.nan

    result = decision_table_validate(envelope, "bad-market")

    assert {
        "schema_version_mismatch",
        "invalid_enum_value",
        "invalid_parent_correlation_id",
        "non_finite_number",
        "decision_table_hash_mismatch",
    } <= _rules(result)
    for violation in result["violations"]:
        assert violation["canonical_shape_ref"].endswith(
            "#canonical-decision-table-shape-v11"
        )
        assert violation["detected_shape"] == result["detected_shape"]


def test_schema_v1_is_advisory_not_block_when_the_shape_is_canonical() -> None:
    envelope = _happy_envelope()
    envelope["schema_version"] = _DEPRECATED_SCHEMA

    result = decision_table_validate(envelope, "kr")

    assert result["valid"] is True
    assert _rules(result) == {"schema_version_deprecated_v1"}


def test_non_object_and_missing_decision_table_never_raise() -> None:
    assert _rules(decision_table_validate([], "kr")) == {"table_not_object"}
    assert _rules(decision_table_validate({"market": "kr"}, "kr")) == {
        "missing_decision_table"
    }


def test_duplicate_conditions_and_enum_fail_closed() -> None:
    envelope = _happy_envelope()
    row = envelope["decision_table"]["rows"][0]
    duplicate = deepcopy(row)
    row["conditions"][0].pop("source")
    row["conditions"][0].pop("max_age_seconds")
    row["conditions"][0]["operator"] = "approximately"
    row["action"].update(
        proposal_action="create", account_mode="unknown", side="hold", order_type="stop"
    )
    envelope["decision_table"]["rows"].append(duplicate)
    _rehash(envelope)

    result = decision_table_validate(envelope, "kr")

    assert {
        "duplicate_scenario_id",
        "invalid_condition_operator",
        "condition_missing_source",
        "condition_missing_max_age_seconds",
        "invalid_enum_value",
    } <= _rules(result)


def test_v11_rung_field_bounds_and_tick_contracts() -> None:
    envelope = _happy_envelope()
    rungs = envelope["decision_table"]["rows"][0]["action"]["rungs"]
    rungs[0].pop("rung")
    rungs.append(
        {
            "rung": 2,
            "price_min": 315000,
            "price_max": 315000,
            "qty": 1.0,
            "tick": 500,
        }
    )
    rungs.append(
        {
            "rung": 3,
            "price_min": 315001,
            "price_max": 315000,
            "qty": 1,
            "tick": 500,
        }
    )
    _rehash(envelope)

    result = decision_table_validate(envelope, "kr")

    assert {
        "rungs_missing_field",
        "rungs_field_not_integer",
        "rungs_price_bounds_inverted",
        "rungs_price_not_tick_aligned",
    } <= _rules(result)


def test_krx_tick_grid_reuses_tick_size_module() -> None:
    envelope = _happy_envelope()
    rung = envelope["decision_table"]["rows"][0]["action"]["rungs"][0]
    rung.update(price_min=5001, price_max=5001, tick=1)
    _rehash(envelope)

    result = decision_table_validate(envelope, "kr")

    assert "tick_grid_violation" in _rules(result)


def test_extensions_are_declared_or_reported_as_advisories() -> None:
    envelope = _happy_envelope()
    table = envelope["decision_table"]
    table["listed_extension"] = {"retained": True}
    table["extensions"] = [
        "common_conditions",
        "listed_extension",
        "absent_extension",
    ]
    table["unlisted_extension"] = {"still": "reported"}
    _rehash(envelope)

    result = decision_table_validate(envelope, "kr")

    assert result["valid"] is True
    assert _rules(result) == {"extensions_entry_absent", "unknown_top_level_key"}
    assert _violation(result, "unknown_top_level_key")["actual"] == "unlisted_extension"


def test_buy_sizing_deep_minimum_and_sector_contracts() -> None:
    envelope = _happy_envelope()
    row = envelope["decision_table"]["rows"][0]
    action = row["action"]
    action["side"] = "buy"
    action["reference_price"] = 1000
    action["minimum_order_amount"] = 270000
    row["sector_concentration"] = {"projected_pct": 11}
    rung = action["rungs"][0]
    rung.update(price_min=900, price_max=900, qty=300, tick=1)
    _rehash(envelope)

    valid_with_advisory = decision_table_validate(envelope, "kr")
    assert valid_with_advisory["valid"] is True
    assert _rules(valid_with_advisory) == {"sector_concentration"}

    rung.update(price_min=1000, price_max=1000, qty=1, tick=1)
    _rehash(envelope)
    result = decision_table_validate(envelope, "kr")

    assert {
        "sizing_band_violation",
        "deep_limit_violation",
        "below_min_order_amount",
        "sector_concentration",
    } <= _rules(result)


def test_prose_scalar_and_parallel_encodings_have_distinct_rules() -> None:
    prose = _happy_envelope()
    prose["decision_table"]["rows"][0]["action"]["rungs"] = "315000원에 1주 매도"
    _rehash(prose)
    assert "price_qty_not_machine_recomputable" in _rules(
        decision_table_validate(prose, "kr")
    )

    scalar = _happy_envelope()
    scalar["decision_table"]["rows"][0]["action"]["rungs"] = {
        "price_min": 315000,
        "qty": 1,
    }
    _rehash(scalar)
    assert "unsupported_rungs_encoding" in _rules(decision_table_validate(scalar, "kr"))

    parallel = _happy_envelope()
    parallel["decision_table"]["rows"][0]["action"]["rungs"] = {
        "count": 1,
        "prices": [315000],
        "quantities": [1],
        "price_min": [315000],
        "price_max": [315000],
        "quantity_min": [1],
        "quantity_max": [1],
    }
    _rehash(parallel)
    assert "unsupported_rungs_encoding" in _rules(
        decision_table_validate(parallel, "kr")
    )


def test_registration_exposes_the_pure_tool_on_analysis_and_readonly_profiles() -> None:
    class FakeMCP:
        def __init__(self) -> None:
            self.registered: dict[str, str] = {}

        def tool(self, *, name: str, description: str, **_: Any) -> Any:
            def decorate(function: Any) -> Any:
                self.registered[name] = description
                return function

            return decorate

    mcp = FakeMCP()
    register_decision_table_tools(mcp)  # type: ignore[arg-type]

    assert DECISION_TABLE_TOOL_NAMES == {"decision_table_validate"}
    assert "decision_table_validate" in ANALYSIS_TOOL_NAMES
    assert "decision_table_validate" in ANALYSIS_READONLY_TOOL_NAMES
    assert "database" in mcp.registered["decision_table_validate"].lower()
    assert "order" in mcp.registered["decision_table_validate"].lower()


def test_validator_package_has_no_database_network_or_broker_import_path() -> None:
    root = Path(__file__).parents[3] / "app" / "services" / "decision_table_validate"
    forbidden_modules = (
        "sqlalchemy",
        "httpx",
        "aiohttp",
        "requests",
        "app.models",
        "app.services.brokers",
        "app.brokers",
        "app.clients.broker",
    )
    forbidden_symbols = {"get_db", "AsyncSession"}

    def is_forbidden_module(module: str) -> bool:
        return "broker" in module.lower() or any(
            module == blocked or module.startswith(f"{blocked}.")
            for blocked in forbidden_modules
        )

    violations: list[str] = []
    for source_path in sorted(root.rglob("*.py")):
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        )
        for node in ast.walk(tree):
            imports: list[str] = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""] + [
                    f"{node.module}.{alias.name}" for alias in node.names if node.module
                ]
                if any(alias.name in forbidden_symbols for alias in node.names):
                    violations.append(
                        f"{source_path}:{node.lineno}: forbidden DB symbol"
                    )
            if any(is_forbidden_module(imported) for imported in imports):
                violations.append(f"{source_path}:{node.lineno}: {imports}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and is_forbidden_module(node.args[0].value)
            ):
                violations.append(f"{source_path}:{node.lineno}: dynamic import")
    assert not violations


def test_static_guard_predicate_catches_importlib_string_bypass() -> None:
    tree = ast.parse("import importlib\nimportlib.import_module('httpx')")
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
    assert isinstance(call.func, ast.Attribute)
    assert isinstance(call.args[0], ast.Constant)
    assert call.args[0].value == "httpx"
    assert importlib.import_module.__name__ == "import_module"
