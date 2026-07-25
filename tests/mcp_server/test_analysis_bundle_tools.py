from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from app.mcp_server.tooling import analysis_bundle_handlers as handlers
from app.services.analysis_snapshot_bundle.read import (
    AnalysisBundleIntegrityError,
    AnalysisBundleNotFound,
    UnknownAnalysisBundleSection,
)


@dataclass
class _RecorderMCP:
    tools: dict[str, Any] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)

    def tool(self, *, name: str, description: str):
        def decorator(func):
            self.tools[name] = func
            self.descriptions[name] = description
            return func

        return decorator


class _SessionContext:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def __aenter__(self) -> Any:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def test_registrar_can_physically_omit_create() -> None:
    mcp = _RecorderMCP()

    handlers.register_analysis_bundle_tools(mcp, allow_create=False)

    assert set(mcp.tools) == {"analysis_bundle_get"}


def test_descriptions_lock_frozen_evidence_boundaries() -> None:
    mcp = _RecorderMCP()
    handlers.register_analysis_bundle_tools(mcp)
    get_description = mcp.descriptions["analysis_bundle_get"]
    create_description = mcp.descriptions["analysis_bundle_create"]

    assert "verbatim" in get_description.lower()
    assert "zero provider" in get_description.lower()
    assert "sha-256" in get_description.lower()
    assert "no order" in create_description.lower()


@pytest.mark.asyncio
async def test_create_delegates_once_and_commits_only_after_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    session = SimpleNamespace()

    async def commit() -> None:
        events.append("commit")

    session.commit = commit
    monkeypatch.setattr(handlers, "AsyncSessionLocal", lambda: _SessionContext(session))

    collectors = object()
    monkeypatch.setattr(
        handlers, "production_collector_registry", lambda db: collectors
    )

    async def fake_analyze(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert args == (["005930"],)
        assert kwargs == {
            "market": "kr",
            "include_peers": False,
            "quick": False,
            "include_position": False,
            "refresh": False,
        }
        return {"analysis": "frozen"}

    async def fake_decision(
        db: Any, symbol: str, market: str, *, account_mode: str | None
    ) -> dict[str, Any]:
        assert db is session
        assert (symbol, market, account_mode) == ("005930", "kr", None)
        return {"decision": "history"}

    monkeypatch.setattr(handlers, "analyze_stock_batch_impl", fake_analyze)
    monkeypatch.setattr(handlers, "build_decision_context", fake_decision)

    response = SimpleNamespace(
        model_dump=lambda *, mode: {
            "bundle_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "status": "complete",
        }
    )
    capture_calls: list[Any] = []

    class FakeCaptureService:
        def __init__(
            self,
            db: Any,
            *,
            collectors: Any,
            analysis_fn: Any,
            decision_history_fn: Any,
        ) -> None:
            assert db is session
            assert collectors is globals_collectors
            self.analysis_fn = analysis_fn
            self.decision_history_fn = decision_history_fn

        async def capture(self, request: Any) -> Any:
            events.append("capture")
            capture_calls.append(request)
            await self.analysis_fn(["005930"], market="kr", include_peers=False)
            await self.decision_history_fn("005930", "kr", None)
            return response

    globals_collectors = collectors
    monkeypatch.setattr(handlers, "AnalysisBundleCaptureService", FakeCaptureService)

    result = await handlers.analysis_bundle_create_impl(
        "kr", None, ["005930"], user_id=7, market_session="regular"
    )

    assert events == ["capture", "commit"]
    assert len(capture_calls) == 1
    assert capture_calls[0].account_scope is None
    assert capture_calls[0].user_id == 7
    assert result == {
        "success": True,
        "bundle_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "status": "complete",
    }


@pytest.mark.asyncio
async def test_create_does_not_commit_when_capture_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(commit=pytest.fail)
    monkeypatch.setattr(handlers, "AsyncSessionLocal", lambda: _SessionContext(session))
    monkeypatch.setattr(handlers, "production_collector_registry", lambda db: object())

    class FailingCaptureService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def capture(self, request: Any) -> Any:
            raise RuntimeError("capture failed")

    monkeypatch.setattr(handlers, "AnalysisBundleCaptureService", FailingCaptureService)

    with pytest.raises(RuntimeError, match="capture failed"):
        await handlers.analysis_bundle_create_impl("kr", None, ["005930"])


@pytest.mark.asyncio
async def test_get_rejects_invalid_uuid_before_opening_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handlers,
        "AsyncSessionLocal",
        lambda: pytest.fail("invalid UUID must not open a consumer DB session"),
    )

    assert await handlers.analysis_bundle_get_impl("not-a-uuid") == {
        "success": False,
        "error": "invalid_bundle_id",
        "bundle_id": "not-a-uuid",
    }


@pytest.mark.asyncio
async def test_get_instantiates_only_read_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = object()
    monkeypatch.setattr(handlers, "AsyncSessionLocal", lambda: _SessionContext(session))
    monkeypatch.setattr(
        handlers,
        "production_collector_registry",
        lambda db: pytest.fail("get must not instantiate provider collectors"),
    )
    response = SimpleNamespace(
        model_dump=lambda *, mode: {"bundle_id": str(uuid.UUID(int=1)), "document": {}}
    )
    calls: list[tuple[Any, Any, Any]] = []

    class FakeReadService:
        def __init__(self, repository: Any) -> None:
            assert repository._session is session

        async def get(self, bundle_id: uuid.UUID, sections: Any) -> Any:
            calls.append((bundle_id, sections, session))
            return response

    monkeypatch.setattr(handlers, "AnalysisBundleReadService", FakeReadService)

    result = await handlers.analysis_bundle_get_impl(
        str(uuid.UUID(int=1)), ["portfolio"]
    )

    assert len(calls) == 1
    assert result == {
        "success": True,
        "bundle_id": str(uuid.UUID(int=1)),
        "document": {},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (AnalysisBundleNotFound("missing"), "analysis_bundle_not_found"),
        (AnalysisBundleIntegrityError("bad hash"), "analysis_bundle_integrity_error"),
        (UnknownAnalysisBundleSection("unknown"), "unknown_analysis_bundle_section"),
    ],
)
async def test_get_maps_structured_service_errors(
    monkeypatch: pytest.MonkeyPatch, exception: Exception, error: str
) -> None:
    session = object()
    monkeypatch.setattr(handlers, "AsyncSessionLocal", lambda: _SessionContext(session))

    class FailingReadService:
        def __init__(self, repository: Any) -> None:
            pass

        async def get(self, bundle_id: uuid.UUID, sections: Any) -> Any:
            raise exception

    monkeypatch.setattr(handlers, "AnalysisBundleReadService", FailingReadService)
    bundle_id = str(uuid.UUID(int=2))

    result = await handlers.analysis_bundle_get_impl(bundle_id, ["portfolio"])

    assert result["success"] is False
    assert result["error"] == error
    assert result["bundle_id"] == bundle_id
