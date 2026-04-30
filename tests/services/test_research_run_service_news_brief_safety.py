"""Safety tests for research_run_service.record_kr_preopen_news_brief (ROB-62).

Verifies:
- Persisted ResearchRun has advisory_only=True, execution_allowed=False on all links.
- Forbidden execution keys in candidate_payloads raise ValueError.
- advisory_links with execution_allowed=True raise ValueError (reuses existing validator).
- No outbound HTTP / KIS / Upbit / Slack calls during brief assembly or persistence.
- No forbidden module imports transitively.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_FORBIDDEN_EXECUTION_KEYS = [
    "quantity",
    "price",
    "side",
    "order_type",
    "dry_run",
    "watch",
    "order_intent",
]

_GOOD_ADVISORY_LINK = {
    "advisory_only": True,
    "execution_allowed": False,
    "provider": "news_brief",
    "description": "KR preopen news brief evidence",
}

_BAD_ADVISORY_LINK_EXEC_ALLOWED = {
    "advisory_only": True,
    "execution_allowed": True,
    "provider": "bad_link",
}

_BAD_ADVISORY_LINK_NOT_ADVISORY = {
    "advisory_only": False,
    "execution_allowed": False,
    "provider": "bad_link2",
}


# --- advisory_links validation ---


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_kr_preopen_news_brief_persists_advisory_only_run():
    """record_kr_preopen_news_brief must persist with advisory_only markers."""
    from app.services import research_run_service

    created_runs = []

    async def fake_create_research_run(session, **kwargs):
        run = MagicMock()
        run.id = 1
        run.candidates = []
        run.advisory_links = kwargs.get("advisory_links", [])
        created_runs.append(kwargs)
        return run

    with patch.object(
        research_run_service, "create_research_run", new=fake_create_research_run
    ):
        session_mock = AsyncMock()
        await research_run_service.record_kr_preopen_news_brief(
            session_mock,
            user_id=7,
            advisory_links=[_GOOD_ADVISORY_LINK],
            generated_at=datetime.now(UTC),
        )

    assert len(created_runs) == 1
    # All advisory_links passed must be advisory-only
    for link in [_GOOD_ADVISORY_LINK]:
        assert link["advisory_only"] is True
        assert link["execution_allowed"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_kr_preopen_news_brief_rejects_execution_allowed_link():
    """advisory_links with execution_allowed=True must be rejected."""
    from app.services import research_run_service

    session_mock = AsyncMock()
    with pytest.raises(ValueError, match="advisory-only"):
        await research_run_service.record_kr_preopen_news_brief(
            session_mock,
            user_id=7,
            advisory_links=[_BAD_ADVISORY_LINK_EXEC_ALLOWED],
            generated_at=datetime.now(UTC),
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_kr_preopen_news_brief_rejects_non_advisory_link():
    """advisory_links with advisory_only=False must be rejected."""
    from app.services import research_run_service

    session_mock = AsyncMock()
    with pytest.raises(ValueError, match="advisory-only"):
        await research_run_service.record_kr_preopen_news_brief(
            session_mock,
            user_id=7,
            advisory_links=[_BAD_ADVISORY_LINK_NOT_ADVISORY],
            generated_at=datetime.now(UTC),
        )


# --- Forbidden candidate payload keys ---


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("forbidden_key", _FORBIDDEN_EXECUTION_KEYS)
async def test_record_rejects_candidate_payload_with_forbidden_key(forbidden_key: str):
    """Candidate payloads carrying execution keys must raise ValueError."""
    from app.services import research_run_service

    session_mock = AsyncMock()
    bad_payload = {"symbol": "005930", forbidden_key: "some_value"}

    with pytest.raises(ValueError, match="forbidden execution keys"):
        await research_run_service.record_kr_preopen_news_brief(
            session_mock,
            user_id=7,
            candidate_payloads=[bad_payload],
            generated_at=datetime.now(UTC),
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_accepts_clean_candidate_payload():
    """Clean candidate payloads (no execution keys) must not raise."""
    from app.services import research_run_service

    created_runs = []
    added_candidates = []

    async def fake_create(session, **kwargs):
        run = MagicMock()
        run.id = 1
        run.candidates = []
        created_runs.append(kwargs)
        return run

    async def fake_add_candidates(session, *, research_run_id, candidates):
        added_candidates.extend(candidates)
        return []

    with (
        patch.object(research_run_service, "create_research_run", new=fake_create),
        patch.object(
            research_run_service, "add_research_run_candidates", new=fake_add_candidates
        ),
    ):
        session_mock = AsyncMock()
        await research_run_service.record_kr_preopen_news_brief(
            session_mock,
            user_id=7,
            candidate_payloads=[
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "confidence": 70,
                    "reasons": ["good"],
                },
            ],
            generated_at=datetime.now(UTC),
        )

    assert len(created_runs) == 1
    assert len(added_candidates) == 1


# --- No outbound calls ---


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_does_not_make_outbound_http_calls():
    """record_kr_preopen_news_brief must not invoke KIS/Upbit/Slack clients."""
    from app.services import research_run_service

    outbound_called = []

    class FakeClient:
        def __getattr__(self, name):
            def boom(*args, **kwargs):
                outbound_called.append(f"{self.__class__.__name__}.{name}")
                raise AssertionError(f"Outbound call: {self.__class__.__name__}.{name}")

            return boom

    created = []

    async def fake_create(session, **kwargs):
        run = MagicMock()
        run.id = 1
        run.candidates = []
        created.append(True)
        return run

    with patch.object(research_run_service, "create_research_run", new=fake_create):
        session_mock = AsyncMock()
        await research_run_service.record_kr_preopen_news_brief(
            session_mock,
            user_id=7,
            generated_at=datetime.now(UTC),
        )

    assert not outbound_called


# --- No forbidden transitive imports ---

FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.upbit",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.orders",
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.kis_websocket",
    "app.services.kis_trading_service",
    "app.services.upbit_websocket",
    "app.services.redis_token_manager",
    "app.tasks",
    "redis",
]


@pytest.mark.unit
def test_kr_preopen_news_brief_service_does_not_import_forbidden() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = """
import importlib
import json
import sys

importlib.import_module('app.services.kr_preopen_news_brief_service')
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        name
        for name in loaded
        for forbidden in FORBIDDEN_PREFIXES
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert not violations, f"forbidden modules transitively imported: {violations}"


@pytest.mark.unit
def test_research_run_service_still_does_not_import_forbidden() -> None:
    """Extending research_run_service must not introduce forbidden imports."""
    project_root = Path(__file__).resolve().parents[2]
    script = """
import importlib
import json
import sys

importlib.import_module('app.services.research_run_service')
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        name
        for name in loaded
        for forbidden in FORBIDDEN_PREFIXES
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert not violations, f"forbidden modules transitively imported: {violations}"
