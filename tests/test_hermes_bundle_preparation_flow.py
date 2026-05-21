"""ROB-287 Phase B — Hermes bundle-preparation Prefect flow tests.

Two test surfaces, mirroring ROB-269 ``investment_snapshots_refresh_flow``:

* **Static** assertions on the flow file (decorators, default purpose,
  no broker-mutation verbs, no deployment YAML).
* **Runtime** assertions on ``run_hermes_bundle_preparation`` — the
  gate-off path returns a structured ``"disabled"`` envelope WITHOUT
  touching ``AsyncSessionLocal`` or ``SnapshotBundleEnsureService``;
  the gate-on path routes through the ensure service.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings

_FLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "flows"
    / "hermes_bundle_preparation_flow.py"
)


# ---------------------------------------------------------------------------
# Static — mirrors ROB-269's flow-file invariants
# ---------------------------------------------------------------------------


def test_flow_file_exists() -> None:
    assert _FLOW_PATH.exists(), f"Flow file not found at {_FLOW_PATH}"


def test_flow_file_declares_prefect_flow_and_task() -> None:
    text = _FLOW_PATH.read_text()
    assert "@flow" in text, "Missing @flow decorator"
    assert "@task" in text, "Missing @task decorator"
    assert "hermes_bundle_preparation_flow" in text, "Missing flow function"
    assert "hermes_bundle_preparation_task" in text, "Missing task function"
    assert "run_hermes_bundle_preparation" in text, "Missing coroutine body"


def test_flow_file_defaults_to_hermes_purpose() -> None:
    text = _FLOW_PATH.read_text()
    assert 'purpose: str = "hermes_report_generation"' in text, (
        "Flow must default to purpose='hermes_report_generation'"
    )
    assert 'requested_by: str = "hermes"' in text, (
        "Flow must mark ensure runs with requested_by='hermes'"
    )
    assert 'policy_version: str = "intraday_action_report_v1"' in text


def test_flow_file_uses_ensure_fresh_mode() -> None:
    text = _FLOW_PATH.read_text()
    assert 'mode="ensure_fresh"' in text, (
        "Flow must call SnapshotBundleEnsureService with mode='ensure_fresh'"
    )


def test_flow_file_imports_ensure_service() -> None:
    text = _FLOW_PATH.read_text()
    assert "SnapshotBundleEnsureService" in text


def test_flow_file_does_not_call_broker_mutation_verbs() -> None:
    text = _FLOW_PATH.read_text()
    for verb in (
        "submit_order(",
        "cancel_order(",
        "modify_order(",
        "place_order(",
        "create_watch_intent(",
    ):
        assert verb not in text, f"Flow file contains forbidden verb: {verb!r}"


def test_flow_file_references_operational_gate() -> None:
    """The flow must consult HERMES_BUNDLE_PREPARATION_ENABLED — that's the
    whole point of Phase B."""
    text = _FLOW_PATH.read_text()
    assert "HERMES_BUNDLE_PREPARATION_ENABLED" in text


def test_flow_is_not_registered_via_deployment_yaml() -> None:
    """Deployment registration lives in robin-prefect-automations and is
    operator-gated. Fail loudly if a YAML in this repo references the
    flow name."""
    project_root = _FLOW_PATH.parents[2]
    yaml_files = list(project_root.glob("**/*.yaml")) + list(
        project_root.glob("**/*.yml")
    )
    for yf in yaml_files:
        if ".venv" in str(yf) or ".git" in str(yf) or "node_modules" in str(yf):
            continue
        try:
            content = yf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        assert "hermes_bundle_preparation_flow" not in content, (
            f"Found Prefect deployment YAML referencing the Hermes prep flow "
            f"at {yf}. Activation is deferred to robin-prefect-automations."
        )


# ---------------------------------------------------------------------------
# Runtime — gate-off / gate-on behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_off_returns_disabled_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "HERMES_BUNDLE_PREPARATION_ENABLED", False, raising=False
    )
    from app.flows.hermes_bundle_preparation_flow import (
        run_hermes_bundle_preparation,
    )

    result = await run_hermes_bundle_preparation(market="kr")
    assert result["status"] == "disabled"
    assert result["gate"] == "HERMES_BUNDLE_PREPARATION_ENABLED"
    assert "HERMES_BUNDLE_PREPARATION_ENABLED" in result["message"]


@pytest.mark.asyncio
async def test_gate_off_does_not_open_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When disabled, the flow must not touch ``AsyncSessionLocal`` or the
    ensure service. Proves the dry-run is genuinely side-effect-free."""
    monkeypatch.setattr(
        settings, "HERMES_BUNDLE_PREPARATION_ENABLED", False, raising=False
    )
    from app.flows.hermes_bundle_preparation_flow import (
        run_hermes_bundle_preparation,
    )

    session_factory = AsyncMock()
    ensure_service = AsyncMock()

    with (
        patch(
            "app.flows.hermes_bundle_preparation_flow._session_factory",
            return_value=session_factory,
        ),
        patch(
            "app.flows.hermes_bundle_preparation_flow.SnapshotBundleEnsureService",
            return_value=ensure_service,
        ),
    ):
        result = await run_hermes_bundle_preparation()

    assert result["status"] == "disabled"
    session_factory.assert_not_called()
    ensure_service.ensure.assert_not_awaited()


class _FakeAsyncSession:
    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def _session_factory_returning_fake() -> object:
    def _factory() -> _FakeAsyncSession:
        return _FakeAsyncSession()

    return _factory


@pytest.mark.asyncio
async def test_gate_on_routes_through_ensure_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    monkeypatch.setattr(
        settings, "HERMES_BUNDLE_PREPARATION_ENABLED", True, raising=False
    )

    bundle_uuid = uuid.uuid4()
    ensure_response = SimpleNamespace(
        bundle_uuid=bundle_uuid,
        status="complete",
        freshness_summary={"overall": "fresh"},
        coverage_summary={"news": {"status": "fresh"}},
        missing_sources=[],
        warnings=[],
        created=True,
    )
    ensure_service = AsyncMock()
    ensure_service.ensure = AsyncMock(return_value=ensure_response)

    from app.flows.hermes_bundle_preparation_flow import (
        run_hermes_bundle_preparation,
    )

    with (
        patch(
            "app.flows.hermes_bundle_preparation_flow._session_factory",
            return_value=_session_factory_returning_fake(),
        ),
        patch(
            "app.flows.hermes_bundle_preparation_flow.SnapshotBundleEnsureService",
            return_value=ensure_service,
        ),
    ):
        result = await run_hermes_bundle_preparation(
            market="kr",
            account_scope="kis_live",
            symbols=["005930"],
        )

    assert result["status"] == "ok"
    assert result["bundle_uuid"] == str(bundle_uuid)
    assert result["bundle_status"] == "complete"
    assert result["freshness_summary"] == {"overall": "fresh"}
    assert result["created"] is True
    assert result["request_envelope"]["purpose"] == "hermes_report_generation"
    assert result["request_envelope"]["requested_by"] == "hermes"

    ensure_service.ensure.assert_awaited_once()
    called_request = ensure_service.ensure.call_args.args[0]
    assert called_request.purpose == "hermes_report_generation"
    assert called_request.market == "kr"
    assert called_request.account_scope == "kis_live"
    assert called_request.symbols == ["005930"]
    assert called_request.requested_by == "hermes"


@pytest.mark.asyncio
async def test_settings_default_is_disabled() -> None:
    """Smoke check on the config default — flipping this default in code
    would silently activate the flow on every Prefect worker. Guard
    against it."""
    from app.core.config import Settings

    fresh = Settings()
    assert fresh.HERMES_BUNDLE_PREPARATION_ENABLED is False
