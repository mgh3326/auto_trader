"""
Tests for API routers.
"""

import pytest
from fastapi.testclient import TestClient

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


class TestActiveSurfaceRouter:
    """Test active surface endpoints."""

    def test_get_screener(self, client):
        """Test screener page endpoint."""
        response = client.get("/screener")
        assert response.status_code == 200

    def test_get_portfolio(self, client):
        """Test portfolio page endpoint."""
        response = client.get("/portfolio/")
        assert response.status_code == 200


class TestRouterIntegration:
    """Test router integration."""

    def test_router_registration(self, client):
        """Test that all routers are properly registered."""
        # Test that the main app has the expected routers
        app = client.app
        routes = [route.path for route in app.routes]

        # Check that expected routes exist
        assert any("/healthz" in route for route in routes)
        assert any("/screener" in route for route in routes)
        assert any("/portfolio" in route for route in routes)
        assert not any("/dashboard" in route for route in routes)
