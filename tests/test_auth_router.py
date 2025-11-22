from unittest.mock import MagicMock
from app.auth.security import get_password_hash
from app.models.trading import User


def test_register_user_success(auth_test_client, auth_mock_session):
    # Setup mock to return None for existing user check (username and email)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    auth_mock_session.execute.return_value = mock_result

    response = auth_test_client.post(
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


def test_register_user_duplicate_username(auth_test_client, auth_mock_session):
    # Setup mock to return a user for username check
    mock_result = MagicMock()
    existing_user = User(id=1, username="testuser", email="other@example.com")
    mock_result.scalar_one_or_none.return_value = existing_user
    auth_mock_session.execute.return_value = mock_result

    response = auth_test_client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "username": "testuser",
            "password": "password123"
        }
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Username already registered"


def test_login_success(auth_test_client, auth_mock_session):
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
    auth_mock_session.execute.return_value = mock_result

    response = auth_test_client.post(
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


def test_login_invalid_credentials(auth_test_client, auth_mock_session):
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
    auth_mock_session.execute.return_value = mock_result

    response = auth_test_client.post(
        "/auth/login",
        data={
            "username": "testuser",
            "password": "wrongpassword"
        }
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect username or password"
