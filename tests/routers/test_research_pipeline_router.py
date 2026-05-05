from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.routers.dependencies import get_authenticated_user


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = 1
    user.username = "testuser"
    return user

@pytest.fixture
def override_deps(mock_user):
    app.dependency_overrides[get_authenticated_user] = lambda: mock_user
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    # Patch AuthMiddleware to bypass authentication
    with patch("app.middleware.auth.AuthMiddleware._maybe_authenticate", return_value=None):
        yield

    app.dependency_overrides = {}

@pytest.mark.asyncio
async def test_router_forbidden_when_disabled(override_deps):
    with patch.object(settings, "RESEARCH_PIPELINE_ENABLED", False):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/api/research-pipeline/sessions")
            assert response.status_code == status.HTTP_403_FORBIDDEN

@pytest.mark.asyncio
async def test_get_sessions_list(override_deps):
    with patch.object(settings, "RESEARCH_PIPELINE_ENABLED", True):
        # We need to patch the method on the class because it's instantiated in the router
        with patch("app.routers.research_pipeline.ResearchPipelineService.list_recent_sessions", new_callable=AsyncMock) as mock_service:
            mock_list = [{"id": 1, "status": "finalized"}]
            mock_service.return_value = mock_list
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.get("/api/research-pipeline/sessions")
                assert response.status_code == status.HTTP_200_OK
                assert len(response.json()) == 1

@pytest.mark.asyncio
async def test_get_session_by_id(override_deps):
    with patch.object(settings, "RESEARCH_PIPELINE_ENABLED", True):
        mock_session = {"id": 1, "status": "open", "stock_info_id": 123}
        with patch("app.routers.research_pipeline.ResearchPipelineService.get_session", new_callable=AsyncMock) as mock_service:
            mock_service.return_value = mock_session
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.get("/api/research-pipeline/sessions/1")
                assert response.status_code == status.HTTP_200_OK
                assert response.json()["id"] == 1

@pytest.mark.asyncio
async def test_get_session_stages(override_deps):
    with patch.object(settings, "RESEARCH_PIPELINE_ENABLED", True):
        mock_stages = [{"id": 1, "stage_type": "market"}]
        with patch("app.routers.research_pipeline.ResearchPipelineService.get_latest_stages", new_callable=AsyncMock) as mock_service:
            mock_service.return_value = mock_stages
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.get("/api/research-pipeline/sessions/1/stages")
                assert response.status_code == status.HTTP_200_OK
                assert len(response.json()) == 1

@pytest.mark.asyncio
async def test_get_session_summary(override_deps):
    with patch.object(settings, "RESEARCH_PIPELINE_ENABLED", True):
        mock_summary = {"id": 1, "decision": "buy", "confidence": 80}
        with patch("app.routers.research_pipeline.ResearchPipelineService.get_latest_summary", new_callable=AsyncMock) as mock_service:
            mock_service.return_value = mock_summary
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.get("/api/research-pipeline/sessions/1/summary")
                assert response.status_code == status.HTTP_200_OK
                assert response.json()["decision"] == "buy"
