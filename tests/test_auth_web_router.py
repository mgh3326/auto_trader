from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auth.security import get_password_hash
from app.auth.web_router import MAX_SESSIONS_PER_USER
from app.models.trading import User, UserRole


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


# ==================== 다중 세션 로그인 테스트 ====================


@pytest.fixture
def mock_limiter():
    """Rate limiter를 비활성화하는 fixture"""
    from app.auth import web_router

    # limiter의 enabled 속성을 False로 설정
    original_enabled = web_router.limiter.enabled
    web_router.limiter.enabled = False
    yield
    web_router.limiter.enabled = original_enabled


class TestMultipleSessionLogin:
    """다중 디바이스 로그인 테스트"""

    @pytest.fixture(autouse=True)
    def disable_rate_limit(self, mock_limiter):
        """이 클래스의 모든 테스트에서 rate limiter 비활성화"""
        pass

    @pytest.fixture
    def mock_redis_client(self):
        """Redis 클라이언트 mock"""
        mock_client = AsyncMock()
        mock_client.sismember = AsyncMock(return_value=True)
        mock_client.sadd = AsyncMock(return_value=1)
        mock_client.srem = AsyncMock(return_value=1)
        mock_client.scard = AsyncMock(return_value=0)
        mock_client.spop = AsyncMock(return_value=None)
        mock_client.expire = AsyncMock(return_value=True)
        mock_client.get = AsyncMock(return_value=None)
        mock_client.set = AsyncMock(return_value=True)
        mock_client.aclose = AsyncMock()
        return mock_client

    @pytest.fixture
    def test_user(self):
        """테스트용 사용자"""
        return User(
            id=1,
            username="testuser",
            email="test@example.com",
            hashed_password=get_password_hash("password123"),
            role=UserRole.viewer,
            is_active=True
        )

    def test_multiple_logins_use_redis_set(self, auth_test_client, auth_mock_session, test_user, mock_redis_client):
        """여러 번 로그인해도 Redis Set에 세션 추가"""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = test_user
        auth_mock_session.execute.return_value = mock_result

        with patch("app.auth.web_router.redis.from_url", return_value=mock_redis_client):
            # 첫 번째 로그인
            response1 = auth_test_client.post(
                "/web-auth/login",
                data={"username": "testuser", "password": "password123"},
                follow_redirects=False
            )
            assert response1.status_code == 303
            assert "session" in response1.cookies

            # Redis sadd가 호출되었는지 확인 (Set에 추가)
            assert mock_redis_client.sadd.call_count >= 1

    def test_logout_removes_only_current_session(self, auth_test_client, auth_mock_session, test_user, mock_redis_client):
        """로그아웃 시 현재 세션만 Set에서 제거"""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = test_user
        auth_mock_session.execute.return_value = mock_result

        # logout 함수 내에서 redis를 import하므로 redis.asyncio 모듈 자체를 mock
        with patch("app.auth.web_router.redis.from_url", return_value=mock_redis_client), \
             patch("redis.asyncio.from_url", return_value=mock_redis_client):
            # 로그인
            login_response = auth_test_client.post(
                "/web-auth/login",
                data={"username": "testuser", "password": "password123"},
                follow_redirects=False
            )
            session_cookie = login_response.cookies.get("session")

            # 로그아웃
            auth_test_client.cookies.set("session", session_cookie)
            auth_test_client.get("/web-auth/logout", follow_redirects=False)

            # Redis srem이 호출되었는지 확인 (Set에서 제거)
            assert mock_redis_client.srem.called

    def test_session_limit_removes_oldest_when_exceeded(self, auth_test_client, auth_mock_session, test_user):
        """세션 개수가 MAX_SESSIONS_PER_USER를 초과하면 기존 세션 제거"""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = test_user
        auth_mock_session.execute.return_value = mock_result

        # 별도의 mock_redis_client 생성 (scard가 MAX_SESSIONS_PER_USER 반환)
        mock_redis = AsyncMock()
        mock_redis.scard = AsyncMock(return_value=MAX_SESSIONS_PER_USER)
        mock_redis.spop = AsyncMock(return_value="old_session_hash")
        mock_redis.sadd = AsyncMock(return_value=1)
        mock_redis.expire = AsyncMock(return_value=True)
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.aclose = AsyncMock()

        with patch("app.auth.web_router.redis.from_url", return_value=mock_redis):
            # 새 로그인 시도
            response = auth_test_client.post(
                "/web-auth/login",
                data={"username": "testuser", "password": "password123"},
                follow_redirects=False
            )
            assert response.status_code == 303

            # spop이 호출되어 기존 세션 하나 제거
            assert mock_redis.spop.called

    def test_session_validation_uses_sismember(self, auth_test_client, auth_mock_session, mock_auth_middleware_db, test_user, mock_redis_client):
        """세션 검증 시 sismember로 Set 멤버십 확인"""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = test_user
        auth_mock_session.execute.return_value = mock_result
        mock_auth_middleware_db.execute.return_value = mock_result

        # sismember가 True를 반환하도록 설정
        mock_redis_client.sismember = AsyncMock(return_value=True)

        with patch("app.auth.web_router.redis.from_url", return_value=mock_redis_client), \
             patch("app.auth.web_router.get_session_blacklist") as mock_blacklist:
            mock_blacklist.return_value.is_blacklisted = AsyncMock(return_value=False)

            # 로그인
            login_response = auth_test_client.post(
                "/web-auth/login",
                data={"username": "testuser", "password": "password123"},
                follow_redirects=False
            )
            session_cookie = login_response.cookies.get("session")

            # 세션으로 인증 필요한 페이지 접근
            auth_test_client.cookies.set("session", session_cookie)
            # 로그인 페이지는 이미 로그인 상태면 리다이렉트
            response = auth_test_client.get("/web-auth/login", follow_redirects=False)

            # sismember가 호출되었는지 확인
            assert mock_redis_client.sismember.called

    def test_invalid_session_returns_none(self, auth_test_client, auth_mock_session, mock_auth_middleware_db, test_user, mock_redis_client):
        """Set에 없는 세션은 인증 실패"""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = test_user
        auth_mock_session.execute.return_value = mock_result
        mock_auth_middleware_db.execute.return_value = mock_result

        # sismember가 False를 반환 (세션이 Set에 없음)
        mock_redis_client.sismember = AsyncMock(return_value=False)

        with patch("app.auth.web_router.redis.from_url", return_value=mock_redis_client), \
             patch("app.auth.web_router.get_session_blacklist") as mock_blacklist:
            mock_blacklist.return_value.is_blacklisted = AsyncMock(return_value=False)

            # 유효하지 않은 세션으로 접근
            auth_test_client.cookies.set("session", "invalid_session_token")
            response = auth_test_client.get("/web-auth/login", follow_redirects=False)

            # 로그인 페이지가 표시됨 (세션 무효)
            assert response.status_code == 200
            assert "로그인" in response.text


def test_max_sessions_per_user_constant():
    """MAX_SESSIONS_PER_USER 상수가 올바르게 설정되어 있는지 확인"""
    assert MAX_SESSIONS_PER_USER == 5
