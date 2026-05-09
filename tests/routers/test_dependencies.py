"""Unit tests for app/routers/dependencies.py auth helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(*, state_user=None):
    """Build a minimal fake Request with controllable state.user."""
    req = SimpleNamespace()
    req.state = SimpleNamespace()
    req.state.user = state_user
    return req


def _make_db():
    return AsyncMock()


# ---------------------------------------------------------------------------
# get_authenticated_user
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_authenticated_user_returns_state_user():
    """If request.state.user is set, return it immediately."""
    from app.routers.dependencies import get_authenticated_user

    fake_user = SimpleNamespace(id=1)
    request = _make_request(state_user=fake_user)

    result = await get_authenticated_user(request=request, db=_make_db())

    assert result is fake_user


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_authenticated_user_falls_back_to_session():
    """If state.user is absent but session resolves, return session user."""
    from app.routers.dependencies import get_authenticated_user

    session_user = SimpleNamespace(id=2)
    request = _make_request(state_user=None)

    with patch(
        "app.routers.dependencies.get_current_user_from_session",
        new=AsyncMock(return_value=session_user),
    ):
        result = await get_authenticated_user(request=request, db=_make_db())

    assert result is session_user


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_authenticated_user_raises_401_when_no_user():
    """If both state.user and session are absent, raise HTTP 401."""
    from app.routers.dependencies import get_authenticated_user

    request = _make_request(state_user=None)

    with patch(
        "app.routers.dependencies.get_current_user_from_session",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_authenticated_user(request=request, db=_make_db())

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "로그인이 필요합니다."


# ---------------------------------------------------------------------------
# get_user_from_request
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_user_from_request_returns_state_user():
    """If request.state.user is set, return it immediately."""
    from app.routers.dependencies import get_user_from_request

    fake_user = SimpleNamespace(id=3)
    request = _make_request(state_user=fake_user)

    result = await get_user_from_request(request=request, db=_make_db())

    assert result is fake_user


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_user_from_request_falls_back_to_session():
    """If state.user is absent but session resolves, return session user."""
    from app.routers.dependencies import get_user_from_request

    session_user = SimpleNamespace(id=4)
    request = _make_request(state_user=None)

    with patch(
        "app.routers.dependencies.get_current_user_from_session",
        new=AsyncMock(return_value=session_user),
    ):
        result = await get_user_from_request(request=request, db=_make_db())

    assert result is session_user


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_user_from_request_raises_401_when_no_user():
    """If both state.user and session are absent, raise HTTP 401."""
    from app.routers.dependencies import get_user_from_request

    request = _make_request(state_user=None)

    with patch(
        "app.routers.dependencies.get_current_user_from_session",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_user_from_request(request=request, db=_make_db())

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Authentication required"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_functions_have_different_401_messages():
    """Regression: the two functions must keep their distinct error messages."""
    from app.routers.dependencies import (
        get_authenticated_user,
        get_user_from_request,
    )

    request = _make_request(state_user=None)

    with patch(
        "app.routers.dependencies.get_current_user_from_session",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc_a:
            await get_authenticated_user(request=request, db=_make_db())
        with pytest.raises(HTTPException) as exc_b:
            await get_user_from_request(request=request, db=_make_db())

    assert exc_a.value.detail != exc_b.value.detail
