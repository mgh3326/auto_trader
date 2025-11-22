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
    mock_session.add = MagicMock()
    mock_session.commit.return_value = None
    
    def side_effect_refresh(instance):
        instance.id = 1
    
    mock_session.refresh.side_effect = side_effect_refresh
    return mock_session

def test_register_user_success(client):
    # Setup mock to return None for existing user check (username and email)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    response = client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "username": "testuser",
            "password": "password123"
        }
    )
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["username"] == "testuser"
    assert "id" in data

def test_register_user_duplicate_username(client):
    # Setup mock to return a user for username check
    mock_result = MagicMock()
    existing_user = User(id=1, username="testuser", email="other@example.com")
    mock_result.scalar_one_or_none.return_value = existing_user
    mock_session.execute.return_value = mock_result

    response = client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "username": "testuser",
            "password": "password123"
        }
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Username already registered"

def test_login_success(client):
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
        "/auth/login",
        data={
            "username": "testuser",
            "password": "password123"
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"

def test_login_invalid_credentials(client):
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
        "/auth/login",
        data={
            "username": "testuser",
            "password": "wrongpassword"
        }
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect username or password"
