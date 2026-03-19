import re
from unittest.mock import MagicMock

from app.auth.security import get_password_hash
from app.models.trading import User


def _extract_csrf(response) -> str:
    cookie_token = response.cookies.get("csrftoken")
    assert cookie_token

    match = re.search(r'name="csrftoken" value="([^"]+)"', response.text)
    assert match
    assert match.group(1) == cookie_token
    return cookie_token


def test_login_page_sets_csrf_cookie_and_hidden_field(auth_test_client):
    response = auth_test_client.get("/web-auth/login")

    assert response.status_code == 200
    assert "csrftoken" in response.cookies
    assert 'name="csrftoken"' in response.text


def test_register_page_sets_csrf_cookie_and_hidden_field(auth_test_client):
    response = auth_test_client.get("/web-auth/register")

    assert response.status_code == 200
    assert "csrftoken" in response.cookies
    assert 'name="csrftoken"' in response.text


def test_login_without_csrf_token_returns_403(auth_test_client):
    response = auth_test_client.post(
        "/web-auth/login",
        data={"username": "testuser", "password": "password123"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_register_without_csrf_token_returns_403(auth_test_client):
    response = auth_test_client.post(
        "/web-auth/register",
        data={
            "email": "new@example.com",
            "username": "newuser",
            "password": "Password1!",
            "password_confirm": "Password1!",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_login_with_valid_csrf_token_succeeds(
    auth_test_client,
    auth_mock_session,
):
    user = User(
        id=1,
        username="testuser",
        email="test@example.com",
        hashed_password=get_password_hash("password123"),
        is_active=True,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    auth_mock_session.execute.return_value = mock_result

    page = auth_test_client.get("/web-auth/login")
    token = _extract_csrf(page)

    response = auth_test_client.post(
        "/web-auth/login",
        data={
            "username": "testuser",
            "password": "password123",
            "csrftoken": token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "session" in response.cookies


def test_register_with_valid_csrf_token_reaches_handler(
    auth_test_client,
    auth_mock_session,
):
    empty_result = MagicMock()
    empty_result.scalar_one_or_none.return_value = None
    auth_mock_session.execute.side_effect = [empty_result, empty_result]

    page = auth_test_client.get("/web-auth/register")
    token = _extract_csrf(page)

    response = auth_test_client.post(
        "/web-auth/register",
        data={
            "email": "new@example.com",
            "username": "newuser",
            "password": "Password1!",
            "password_confirm": "Password1!",
            "csrftoken": token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 201
    assert "회원가입이 완료되었습니다!" in response.text


def test_auth_api_post_is_exempt_from_csrf(auth_test_client):
    response = auth_test_client.post("/auth/login", data={})

    assert response.status_code != 403
