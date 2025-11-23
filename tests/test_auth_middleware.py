import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.models.trading import User
from app.auth.web_router import create_session_token

# Allow /api/data as a public API path for testing the allowlist behavior
settings.PUBLIC_API_PATHS = ["/api/data"]

# Create a separate app for middleware testing to avoid side effects
app = FastAPI()
app.add_middleware(AuthMiddleware)

@app.get("/test-protected", response_class=HTMLResponse)
async def protected_route(request: Request):
    return "Protected Content"

@app.get("/web-auth/login", response_class=HTMLResponse)
async def login_page():
    return "Login Page"

@app.get("/api/data")
async def api_data():
    return {"data": "ok"}

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def mock_db_session():
    return AsyncMock()

@pytest.fixture
def mock_session_local(mock_db_session):
    with patch("app.middleware.auth.AsyncSessionLocal") as mock:
        mock.return_value.__aenter__.return_value = mock_db_session
        yield mock

def test_public_path_access(client, mock_session_local):
    response = client.get("/web-auth/login")
    assert response.status_code == 200
    assert response.text == "Login Page"

def test_api_path_access(client, mock_session_local):
    response = client.get("/api/data")
    assert response.status_code == 200
    assert response.json() == {"data": "ok"}

def test_protected_route_no_auth(client, mock_session_local):
    # Should redirect to login
    response = client.get("/test-protected", follow_redirects=False)
    assert response.status_code == 303
    assert "/web-auth/login" in response.headers["location"]

def test_protected_route_with_auth(client, mock_session_local, mock_db_session):
    # Setup mock user
    user = User(id=1, username="testuser", is_active=True)
    
    # Mock database query
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db_session.execute.return_value = mock_result

    # Create session token
    token = create_session_token(1)
    
    # Set cookie
    client.cookies.set("session", token)
    
    # Mock Redis and Blacklist to ensure fallback to DB
    with patch("app.auth.web_router.get_session_blacklist") as mock_blacklist, \
         patch("app.auth.web_router.redis.from_url") as mock_redis:
        
        # Blacklist check returns False (not blacklisted)
        mock_blacklist.return_value.is_blacklisted = AsyncMock(return_value=False)
        
        # Redis raises exception to trigger "redis_error = True" path
        # which allows fallback to DB in non-production environment
        mock_redis.side_effect = Exception("Redis connection failed")
        
        response = client.get("/test-protected")
        assert response.status_code == 200
        assert response.text == "Protected Content"
