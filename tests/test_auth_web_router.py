import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
from app.main import api
from app.core.db import get_db
from app.auth.security import get_password_hash
from app.models.trading import User

# Mock DB Session
mock_session = AsyncMock()

# Mock DB Session
mock_session = AsyncMock()

@pytest.fixture
def client():
    async def override_get_db():
        yield mock_session
    api.dependency_overrides[get_db] = override_get_db
    yield TestClient(api)
    del api.dependency_overrides[get_db]

@pytest.fixture(autouse=True)
def reset_mock_db():
    mock_session.reset_mock()
    # Default behavior for execute: return a mock result
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result
    return mock_session

def test_login_page_render(client):
    response = client.get("/web-auth/login")
    if response.status_code != 200:
        print(f"Response: {response.text}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "로그인" in response.text

def test_register_page_render(client):
    response = client.get("/web-auth/register")
    if response.status_code != 200:
        print(f"Response: {response.text}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "회원가입" in response.text

def test_web_login_success(client):
    # Setup mock to return a user
    hashed_password = get_password_hash("password123")
    user = User(
        id=1, 
        username="testuser", 
        email="test@example.com", 
        hashed_password=hashed_password,
        is_active=True
    )
    
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_session.execute.return_value = mock_result

    response = client.post(
        "/web-auth/login",
        data={
            "username": "testuser",
            "password": "password123"
        },
        follow_redirects=False
    )
    assert response.status_code == 303
    assert "session" in response.cookies

def test_web_login_failure(client):
    # Setup mock to return a user
    hashed_password = get_password_hash("password123")
    user = User(
        id=1, 
        username="testuser", 
        email="test@example.com", 
        hashed_password=hashed_password,
        is_active=True
    )
    
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_session.execute.return_value = mock_result

    response = client.post(
        "/web-auth/login",
        data={
            "username": "testuser",
            "password": "wrongpassword"
        }
    )
    if response.status_code != 400:
        print(f"Response: {response.text}")
    assert response.status_code == 400
    assert "사용자명 또는 비밀번호가 올바르지 않습니다" in response.text

def test_web_logout(client):
    response = client.get("/web-auth/logout", follow_redirects=False)
    assert response.status_code == 303
    # Check if session cookie is deleted (expired)
    # TestClient handles cookies differently, but we can check if it's cleared in the jar or response headers
    # A simple check is to see if Set-Cookie header is present with empty value or past expiry
    assert "session" not in response.cookies or response.cookies["session"] == ""
