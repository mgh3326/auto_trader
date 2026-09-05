"""Acceptance tests for ROB-1349's resumable decision-table application."""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.analysis_artifact_tools import (
    analysis_artifact_get,
    analysis_artifact_list,
    analysis_artifact_save,
)
from app.mcp_server.tooling.decision_table_apply_registration import (
    register_decision_table_apply_tools,
)
from app.models.analysis_artifact import AnalysisArtifact
from app.models.investment_reports import InvestmentWatchAlert
from app.models.order_proposals import OrderProposal
from app.models.review import TradeForecast
from app.schemas.analysis_artifact import AnalysisArtifactGetResponse
from app.services.decision_table_apply import (
    DecisionTableApplyDependencies,
    apply_decision_table,
)
from tests._mcp_tooling_support import DummyMCP
from tests.mcp_server._registration_recorder import collect_profile_tools

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[3]
_TESTS_ROOT = Path(__file__).parents[2]
_APPLY_FIXTURE_DIR = _TESTS_ROOT / "fixtures" / "decision_table_apply"
_VALIDATE_FIXTURE_DIR = _TESTS_ROOT / "fixtures" / "decision_table_validate"
_HISTORICAL_FIXTURES = (
    "kr-nxt-decision-table-2026-09-02.json",
    "kr-nxt-decision-table-2026-09-03.json",
    "kr-nxt-decision-table-2026-09-04.json",
)


def _canonical_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _happy_response() -> dict[str, Any]:
    fixture = json.loads(
        (_APPLY_FIXTURE_DIR / "kr-nxt-v11-happy-artifact-response.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["_fixture_provenance"] == {
        "kind": "constructed_from_merged_prompt_skeleton",
        "decision_table": "constructed_from_merged_prompt_skeleton",
        "source": "tests/fixtures/decision_table_validate/kr-nxt-decision-table-v11-happy-path.json",
        "real_artifact_claim": False,
    }
    # The response payload is shaped by AnalysisArtifactGetResponse, not a
    # hand-waved test-only artifact type.
    AnalysisArtifactGetResponse.model_validate(fixture["response"])
    return fixture["response"]


def _historical_response(name: str) -> dict[str, Any]:
    """Wrap a ROB-1348 verbatim historical table in the real get response shape."""

    fixture = json.loads((_VALIDATE_FIXTURE_DIR / name).read_text(encoding="utf-8"))
    provenance = fixture["_fixture_provenance"]
    date = provenance["correlation_id"].removeprefix("kr-nxt-prep-")
    envelope = {
        "schema_version": "kr-nxt-decision-table/v1",
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
    response = {
        "success": True,
        "artifact": {
            "id": 2000 + _HISTORICAL_FIXTURES.index(name),
            "artifact_uuid": str(uuid4()),
            "market": "kr",
            "kind": "session_summary",
            "title": f"Historical {date} decision table",
            "symbols": [],
            "as_of": f"{date}T08:00:00+09:00",
            "valid_until": None,
            "session_label": "kr-nxt-prep",
            "correlation_id": provenance["correlation_id"],
            "account_scope": None,
            "content_hash": None,
            "version": 1,
            "readiness_label": None,
            "is_stale": False,
            "created_by": "codex",
            "created_at": f"{date}T08:00:00+09:00",
            "payload": envelope,
            "payload_size_bytes": len(json.dumps(envelope).encode("utf-8")),
        },
    }
    AnalysisArtifactGetResponse.model_validate(response)
    return response


def _rehash(response: dict[str, Any]) -> None:
    payload = response["artifact"]["payload"]
    payload["decision_table_hash"] = _canonical_hash(payload["decision_table"])


def _three_row_response() -> dict[str, Any]:
    response = _happy_response()
    rows = response["artifact"]["payload"]["decision_table"]["rows"]
    first = deepcopy(rows[0])
    second = deepcopy(rows[0])
    third = deepcopy(rows[0])
    first["scenario_id"] = "resume-row-1"
    second["scenario_id"] = "resume-row-2"
    third["scenario_id"] = "resume-row-3"
    rows[:] = [first, second, third]
    _rehash(response)
    return response


class _Harness:
    """In-memory substitute for the independent writer boundaries."""

    def __init__(self, parent_response: dict[str, Any]) -> None:
        self.parent_response = deepcopy(parent_response)
        self.parent_before = deepcopy(parent_response["artifact"])
        self.apply_record: dict[str, Any] | None = None
        self.proposal_calls: list[dict[str, Any]] = []
        self.watch_calls: list[dict[str, Any]] = []
        self.forecast_calls: list[dict[str, Any]] = []
        self.context_calls: list[dict[str, Any]] = []
        self.save_calls: list[dict[str, Any]] = []
        self.failed_scenarios: set[str] = set()
        self._next_id = 1

    @property
    def dependencies(self) -> DecisionTableApplyDependencies:
        return DecisionTableApplyDependencies(
            artifact_get=self.artifact_get,
            artifact_list=self.artifact_list,
            artifact_save=self.artifact_save,
            proposal_create=self.proposal_create,
            watch_create=self.watch_create,
            forecast_save=self.forecast_save,
            context_append=self.context_append,
        )

    def seed_apply_record(
        self,
        *,
        rows: dict[str, dict[str, str]],
        complete: bool = False,
    ) -> None:
        parent = self.parent_response["artifact"]
        payload = parent["payload"]
        self.apply_record = {
            "id": 3000,
            "artifact_uuid": "22222222-2222-4222-8222-222222222222",
            "market": "kr",
            "kind": "session_summary",
            "title": "Decision table apply fixture",
            "symbols": [],
            "as_of": "2026-09-05T08:10:00+09:00",
            "valid_until": None,
            "session_label": "decision_table_apply",
            "correlation_id": "kr-nxt-apply-2026-09-05",
            "account_scope": None,
            "content_hash": None,
            "version": 1,
            "readiness_label": None,
            "is_stale": False,
            "created_by": "system",
            "created_at": "2026-09-05T08:10:00+09:00",
            "payload": {
                "schema": "kr-nxt-apply-record/v1",
                "parent_artifact_uuid": parent["artifact_uuid"],
                "table_hash": payload["decision_table_hash"],
                "rows": deepcopy(rows),
                "complete": complete,
                "at": "2026-09-05T08:10:00+09:00",
            },
            "payload_size_bytes": 0,
        }

    async def artifact_get(self, artifact_id: int | str) -> dict[str, Any]:
        parent = self.parent_response["artifact"]
        if str(artifact_id) in {str(parent["id"]), parent["artifact_uuid"]}:
            return deepcopy(self.parent_response)
        if self.apply_record is not None and str(artifact_id) in {
            str(self.apply_record["id"]),
            self.apply_record["artifact_uuid"],
        }:
            return {"success": True, "artifact": deepcopy(self.apply_record)}
        return {"success": False, "error": "not_found", "artifact_id": artifact_id}

    async def artifact_list(self, **kwargs: Any) -> dict[str, Any]:
        if (
            self.apply_record is None
            or kwargs["correlation_id"] != self.apply_record["correlation_id"]
        ):
            return {"success": True, "artifacts": []}
        record = self.apply_record
        return {
            "success": True,
            "artifacts": [
                {
                    key: record[key]
                    for key in (
                        "id",
                        "artifact_uuid",
                        "market",
                        "kind",
                        "title",
                        "symbols",
                        "as_of",
                        "valid_until",
                        "session_label",
                        "correlation_id",
                        "account_scope",
                        "content_hash",
                        "version",
                        "readiness_label",
                        "is_stale",
                        "created_by",
                        "created_at",
                    )
                }
            ],
        }

    async def artifact_save(self, **kwargs: Any) -> dict[str, Any]:
        self.save_calls.append(deepcopy(kwargs))
        if self.apply_record is None:
            self.seed_apply_record(rows={})
        assert self.apply_record is not None
        self.apply_record["version"] += 1
        self.apply_record["payload"] = deepcopy(kwargs["payload"])
        self.apply_record["title"] = kwargs["title"]
        self.apply_record["symbols"] = kwargs["symbols"]
        self.apply_record["as_of"] = kwargs["as_of"]
        return {
            "success": True,
            "action": "updated",
            "artifact": deepcopy(self.apply_record),
        }

    def _scenario_from_kwargs(self, kwargs: dict[str, Any]) -> str:
        return kwargs["rationale"]["decision_table_apply"]["scenario_id"]

    async def proposal_create(self, **kwargs: Any) -> dict[str, Any]:
        self.proposal_calls.append(deepcopy(kwargs))
        scenario = self._scenario_from_kwargs(kwargs)
        if scenario in self.failed_scenarios:
            return {"success": False, "error": "writer_rejected"}
        identifier = f"proposal-{self._next_id}"
        self._next_id += 1
        return {"success": True, "proposal_id": identifier}

    async def watch_create(self, **kwargs: Any) -> dict[str, Any]:
        self.watch_calls.append(deepcopy(kwargs))
        scenario = kwargs["metadata"]["decision_table_apply"]["scenario_id"]
        if scenario in self.failed_scenarios:
            return {"success": False, "error": "writer_rejected"}
        identifier = f"watch-{self._next_id}"
        self._next_id += 1
        return {"success": True, "alert": {"alert_uuid": identifier}}

    async def forecast_save(self, **kwargs: Any) -> dict[str, Any]:
        self.forecast_calls.append(deepcopy(kwargs))
        identifier = kwargs["forecast_id"]
        return {"success": True, "data": {"forecast_id": identifier}}

    async def context_append(self, **kwargs: Any) -> dict[str, Any]:
        self.context_calls.append(deepcopy(kwargs))
        return {"success": True, "count": 1, "entries": []}


async def _apply(
    harness: _Harness,
    *,
    dry_run: bool = False,
    confirm: bool = True,
) -> dict[str, Any]:
    parent = harness.parent_response["artifact"]
    return await apply_decision_table(
        parent["id"],
        parent["payload"]["decision_table_hash"],
        dry_run=dry_run,
        confirm=confirm,
        dependencies=harness.dependencies,
    )


def test_happy_artifact_fixture_is_real_get_response_shape() -> None:
    response = _happy_response()

    assert response["success"] is True
    assert response["artifact"]["payload"]["schema_version"] == (
        "kr-nxt-decision-table/v1.1"
    )


@pytest.mark.parametrize("gates_enabled", [False, True])
def test_apply_is_limited_to_the_default_helmsman_surface(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool
) -> None:
    profiles = collect_profile_tools(monkeypatch, gates_enabled=gates_enabled)

    registered = {
        profile
        for profile, tools in profiles.items()
        if "decision_table_apply" in tools
    }
    assert registered == {McpProfile.DEFAULT.value}


@pytest.mark.asyncio
async def test_m1_hash_byte_mutation_fails_closed_without_writes() -> None:
    harness = _Harness(_happy_response())
    parent = harness.parent_response["artifact"]
    bad_hash = "0" + parent["payload"]["decision_table_hash"][1:]

    result = await apply_decision_table(
        parent["id"],
        bad_hash,
        dry_run=False,
        confirm=True,
        dependencies=harness.dependencies,
    )

    assert harness.save_calls == harness.proposal_calls == harness.watch_calls == []
    assert harness.forecast_calls == harness.context_calls == []
    assert result["error"] == "table_hash_mismatch"
    assert result["argument_table_hash"] == bad_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _HISTORICAL_FIXTURES)
async def test_m2_historical_v1_fixture_is_blocked_without_writes(name: str) -> None:
    harness = _Harness(_historical_response(name))
    parent = harness.parent_response["artifact"]

    result = await apply_decision_table(
        parent["id"],
        parent["payload"]["decision_table_hash"],
        dry_run=False,
        confirm=True,
        dependencies=harness.dependencies,
    )

    assert harness.save_calls == harness.proposal_calls == harness.watch_calls == []
    assert harness.forecast_calls == harness.context_calls == []
    assert result["error"] == "table_invalid"
    assert result["violations"]


@pytest.mark.asyncio
async def test_m3_real_apply_requires_literal_confirm_before_any_write() -> None:
    harness = _Harness(_happy_response())

    result = await _apply(harness, dry_run=False, confirm=False)

    assert harness.save_calls == harness.proposal_calls == harness.watch_calls == []
    assert harness.forecast_calls == harness.context_calls == []
    assert result == {"success": False, "error": "confirm_required"}


@pytest.mark.asyncio
async def test_m4_complete_record_makes_second_apply_a_duplicate_free_noop() -> None:
    harness = _Harness(_happy_response())

    first = await _apply(harness)
    second = await _apply(harness)

    assert first["complete"] is True
    # Mutant M4 removes _load_apply_record: its second call reaches this
    # assertion with two created proposals instead of one.
    assert len(harness.proposal_calls) == 1
    assert second["already_applied"] is True
    assert second["complete"] is True


@pytest.mark.asyncio
async def test_resumes_only_unmarked_row_in_table_order() -> None:
    harness = _Harness(_three_row_response())
    harness.seed_apply_record(
        rows={
            "resume-row-1": {
                "proposal_id": "existing-1",
                "at": "2026-09-05T08:00:00+09:00",
            },
            "resume-row-2": {
                "proposal_id": "existing-2",
                "at": "2026-09-05T08:01:00+09:00",
            },
        }
    )

    result = await _apply(harness)

    assert [item["status"] for item in result["rows"]] == [
        "skipped",
        "skipped",
        "applied",
    ]
    assert result["already_applied_rows"] == ["resume-row-1", "resume-row-2"]
    assert [
        call["rationale"]["decision_table_apply"]["scenario_id"]
        for call in harness.proposal_calls
    ] == ["resume-row-3"]
    assert result["complete"] is True


@pytest.mark.asyncio
async def test_partial_failure_keeps_order_and_retries_only_failed_row() -> None:
    harness = _Harness(_three_row_response())
    harness.failed_scenarios.add("resume-row-2")

    first = await _apply(harness)

    assert [item["status"] for item in first["rows"]] == [
        "applied",
        "failed",
        "applied",
    ]
    assert first["complete"] is False
    assert [
        call["rationale"]["decision_table_apply"]["scenario_id"]
        for call in harness.proposal_calls
    ] == ["resume-row-1", "resume-row-2", "resume-row-3"]

    harness.failed_scenarios.clear()
    second = await _apply(harness)

    assert [item["status"] for item in second["rows"]] == [
        "skipped",
        "applied",
        "skipped",
    ]
    assert second["complete"] is True
    assert [
        call["rationale"]["decision_table_apply"]["scenario_id"]
        for call in harness.proposal_calls
    ] == ["resume-row-1", "resume-row-2", "resume-row-3", "resume-row-2"]


@pytest.mark.asyncio
async def test_explicit_rung_mapping_uses_pinned_price_for_limit_and_notional() -> None:
    harness = _Harness(_happy_response())

    result = await _apply(harness)

    assert result["complete"] is True
    assert harness.proposal_calls[0]["market"] == "equity_kr"
    assert harness.proposal_calls[0]["rungs"] == [
        {
            "rung_index": 1,
            "side": "sell",
            "quantity": "1",
            "limit_price": "315000",
            "notional": "315000",
        }
    ]


@pytest.mark.asyncio
async def test_watch_and_forecast_rows_use_the_existing_writer_boundaries() -> None:
    response = _three_row_response()
    rows = response["artifact"]["payload"]["decision_table"]["rows"]
    rows[1]["action"].update(
        {
            "apply_kind": "watch",
            "watch": {
                "intent": "sell_review",
                "rationale": "constructed watch fixture",
                "watch_condition": {
                    "metric": "price",
                    "operator": "above",
                    "threshold": "315000",
                },
                "valid_until": "2099-01-02T09:00:00+09:00",
            },
        }
    )
    rows[2]["action"].update(
        {
            "apply_kind": "forecast",
            "forecast": {
                "forecast_target": {
                    "kind": "manual",
                    "direction": "up",
                    "outcome_rule_version": "v1",
                },
                "probability": 0.5,
                "review_date": "2099-01-02",
            },
        }
    )
    _rehash(response)
    harness = _Harness(response)

    result = await _apply(harness)

    assert [item["kind"] for item in result["rows"]] == [
        "proposal",
        "watch",
        "forecast",
    ]
    assert (
        len(harness.proposal_calls)
        == len(harness.watch_calls)
        == len(harness.forecast_calls)
        == 1
    )
    assert harness.watch_calls[0]["idempotency_key"].startswith("decision-table-apply-")
    assert harness.forecast_calls[0]["instrument_type"] == "equity_kr"


@pytest.mark.asyncio
async def test_dynamic_tool_call_loads_no_broker_module() -> None:
    harness = _Harness(_happy_response())
    mcp = DummyMCP()
    register_decision_table_apply_tools(mcp, dependencies=harness.dependencies)
    before = {name for name in sys.modules if name.startswith("app.services.brokers")}

    result = await mcp.tools["decision_table_apply"](
        harness.parent_response["artifact"]["id"],
        harness.parent_response["artifact"]["payload"]["decision_table_hash"],
        dry_run=True,
    )

    loaded_during_call = {
        name for name in sys.modules if name.startswith("app.services.brokers")
    } - before
    assert result["success"] is True
    assert loaded_during_call == set()


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        return None if left is None or right is None else left + right
    return None


def test_static_broker_import_and_dynamic_import_bypass_guard() -> None:
    guarded = [
        *_ROOT.glob("app/services/decision_table_apply/**/*.py"),
        _ROOT / "app/mcp_server/tooling/decision_table_apply_registration.py",
    ]
    bad_imports: list[str] = []
    direct_order_symbols = {
        "place_order",
        "submit_order",
        "kis_live_place_order",
        "toss_place_order",
        "kiwoom_mock_place_order",
        "alpaca_paper_submit_order",
        "binance_demo_submit_order",
    }
    for path in guarded:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(
                        "app.services.brokers"
                    ) or alias.name.split(".")[0] in {"httpx", "aiohttp", "requests"}:
                        bad_imports.append(f"{path}: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("app.services.brokers") or module.split(".")[
                    0
                ] in {
                    "httpx",
                    "aiohttp",
                    "requests",
                }:
                    bad_imports.append(f"{path}: {module}")
                for alias in node.names:
                    if alias.name in direct_order_symbols:
                        bad_imports.append(f"{path}: {alias.name}")
            elif isinstance(node, ast.Call):
                func = node.func
                dynamic_import = (
                    isinstance(func, ast.Name)
                    and func.id in {"__import__", "import_module"}
                ) or (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"__import__", "import_module"}
                )
                if dynamic_import and node.args:
                    target = _literal_string(node.args[0])
                    if (
                        target is None
                        or target.startswith("app.services.brokers")
                        or target.split(".")[0] in {"httpx", "aiohttp", "requests"}
                    ):
                        bad_imports.append(f"{path}: import_module({target!r})")
                if isinstance(func, ast.Name) and func.id in direct_order_symbols:
                    bad_imports.append(f"{path}: {func.id}()")
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr in direct_order_symbols
                ):
                    bad_imports.append(f"{path}: {func.attr}()")
    assert bad_imports == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_writes_no_apply_proposal_watch_or_forecast_and_keeps_prep_immutable(
    db_session: AsyncSession,
) -> None:
    response = _happy_response()
    envelope = deepcopy(response["artifact"]["payload"])
    unique_symbol = f"DTA{uuid4().hex[:8].upper()}"
    envelope["trading_date"] = "2099-01-01"
    envelope["correlation_id"] = "kr-nxt-prep-2099-01-01"
    envelope["decision_table"]["rows"][0]["symbols"] = [unique_symbol]
    _rehash({"artifact": {"payload": envelope}})
    prep = await analysis_artifact_save(
        market="kr",
        kind="session_summary",
        title="ROB-1349 constructed prep fixture",
        symbols=[unique_symbol],
        payload=envelope,
        as_of="2099-01-01T08:00:00+09:00",
        created_by="codex",
        session_label="kr-nxt-prep",
        correlation_id=envelope["correlation_id"],
    )
    assert prep["success"] is True
    before = deepcopy(prep["artifact"])
    no_write_calls: list[dict[str, Any]] = []

    async def unexpected_writer(**kwargs: Any) -> dict[str, Any]:
        no_write_calls.append(kwargs)
        return {"success": False, "error": "unexpected_writer_call"}

    dependencies = DecisionTableApplyDependencies(
        artifact_get=analysis_artifact_get,
        artifact_list=analysis_artifact_list,
        artifact_save=analysis_artifact_save,
        proposal_create=unexpected_writer,
        watch_create=unexpected_writer,
        forecast_save=unexpected_writer,
        context_append=unexpected_writer,
    )
    result = await apply_decision_table(
        prep["artifact"]["id"],
        envelope["decision_table_hash"],
        dry_run=True,
        dependencies=dependencies,
    )

    assert result["success"] is True
    assert no_write_calls == []
    apply_count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AnalysisArtifact)
        .where(AnalysisArtifact.correlation_id == "kr-nxt-apply-2099-01-01")
    )
    proposal_count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(OrderProposal)
        .where(OrderProposal.symbol == unique_symbol)
    )
    watch_count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(InvestmentWatchAlert)
        .where(InvestmentWatchAlert.symbol == unique_symbol)
    )
    forecast_count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(TradeForecast)
        .where(TradeForecast.symbol == unique_symbol)
    )
    assert apply_count == proposal_count == watch_count == forecast_count == 0

    after = await analysis_artifact_get(prep["artifact"]["id"])
    assert after["artifact"]["artifact_uuid"] == before["artifact_uuid"]
    assert after["artifact"]["version"] == before["version"]
    assert after["artifact"]["payload"] == before["payload"]

    async def proposal_writer(**kwargs: Any) -> dict[str, Any]:
        return {"success": True, "proposal_id": "local-proposal-only"}

    async def summary_writer(**kwargs: Any) -> dict[str, Any]:
        return {"success": True, "count": 1, "entries": []}

    applied = await apply_decision_table(
        prep["artifact"]["id"],
        envelope["decision_table_hash"],
        dry_run=False,
        confirm=True,
        dependencies=DecisionTableApplyDependencies(
            artifact_get=analysis_artifact_get,
            artifact_list=analysis_artifact_list,
            artifact_save=analysis_artifact_save,
            proposal_create=proposal_writer,
            watch_create=unexpected_writer,
            forecast_save=unexpected_writer,
            context_append=summary_writer,
        ),
    )
    assert applied["complete"] is True
    after_real_apply = await analysis_artifact_get(prep["artifact"]["id"])
    assert after_real_apply["artifact"]["artifact_uuid"] == before["artifact_uuid"]
    assert after_real_apply["artifact"]["version"] == before["version"]
    assert after_real_apply["artifact"]["payload"] == before["payload"]
