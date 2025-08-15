"""
Tests for API routers.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch
from app.main import api


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(api)


class TestHealthRouter:
    """Test health check endpoints."""

    def test_health_check(self, client):
        """Test health check endpoint."""
        response = client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert data["status"] == "ok"


class TestDashboardRouter:
    """Test dashboard endpoints."""

    def test_get_dashboard_data(self, client):
        """Test dashboard data endpoint."""
        response = client.get("/dashboard/")
        assert response.status_code == 200
        # Add more specific assertions based on your actual endpoint

    def test_get_analysis_list(self, client):
        """Test analysis list endpoint."""
        response = client.get("/dashboard/analysis")
        assert response.status_code == 200
        # Add more specific assertions based on your actual endpoint


class TestRouterIntegration:
    """Test router integration."""

    def test_router_registration(self, client):
        """Test that all routers are properly registered."""
        # Test that the main app has the expected routers
        app = client.app
        routes = [route.path for route in app.routes]
        
        # Check that expected routes exist
        assert any("/healthz" in route for route in routes)
        assert any("/dashboard" in route for route in routes)
        assert any("/analysis" in route for route in routes)
