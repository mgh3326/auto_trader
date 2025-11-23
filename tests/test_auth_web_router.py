from unittest.mock import AsyncMock, MagicMock, patch
from app.auth.security import get_password_hash
from app.models.trading import User


def test_login_page_render(auth_test_client):
    response = auth_test_client.get("/web-auth/login")
    if response.status_code != 200:
        print(f"Response: {response.text}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "로그인" in response.text


def test_register_page_render(auth_test_client):
    response = auth_test_client.get("/web-auth/register")
    if response.status_code != 200:
        print(f"Response: {response.text}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "회원가입" in response.text


def test_web_login_success(auth_test_client, auth_mock_session):
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
        "/web-auth/login",
        data={
            "username": "testuser",
            "password": "password123"
        },
        follow_redirects=False
    )
    assert response.status_code == 303
    assert "session" in response.cookies


def test_web_login_failure(auth_test_client, auth_mock_session):
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


def test_web_logout(auth_test_client, auth_mock_session, mock_auth_middleware_db):
    # Setup mock user for login
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
    
    # Configure BOTH the route dependency DB and the middleware DB
    auth_mock_session.execute.return_value = mock_result
    mock_auth_middleware_db.execute.return_value = mock_result

    # Mock Redis and Blacklist to ensure fallback to DB
    with patch("app.auth.web_router.get_session_blacklist") as mock_blacklist, \
         patch("app.auth.web_router.redis.from_url") as mock_redis:
        
        # Blacklist check returns False (not blacklisted)
        mock_blacklist.return_value.is_blacklisted = AsyncMock(return_value=False)
        
        # Redis raises exception to trigger "redis_error = True" path
        mock_redis.side_effect = Exception("Redis connection failed")

        # Login first
        auth_test_client.post(
            "/web-auth/login",
            data={"username": "testuser", "password": "password123"},
            follow_redirects=False
        )

        # Then logout
        response = auth_test_client.get("/web-auth/logout", follow_redirects=False)
        assert response.status_code == 303
        # Check if session cookie is deleted (expired)
        assert "session" not in response.cookies or response.cookies["session"] == ""
