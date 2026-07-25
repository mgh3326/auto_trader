from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.auth.admin_router import require_admin
from app.main import api
from app.models.trading import User, UserRole


def _user(uid=1, role=UserRole.admin, is_active=True):
    # Create a user model instance.
    user = User(username="u", email="u@x.co", role=role, is_active=is_active)
    user.id = uid
    return user


@pytest.fixture(autouse=True)
def override_refresh_side_effect(auth_mock_session):
    # Override the module-level refresh side-effect from conftest.py
    # to preserve user IDs instead of forcing them to 1.
    def custom_refresh(instance):
        if not getattr(instance, "id", None):
            instance.id = 1

    auth_mock_session.refresh.side_effect = custom_refresh


@pytest.mark.asyncio
async def test_require_admin_unauthenticated_401():
    with patch(
        "app.auth.admin_router.get_current_user_from_session",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await require_admin(MagicMock(), MagicMock())
    assert exc.value.status_code == 401
    assert exc.value.detail == "로그인이 필요합니다."


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.viewer, UserRole.trader])
async def test_require_admin_non_admin_403(role):
    with patch(
        "app.auth.admin_router.get_current_user_from_session",
        new=AsyncMock(return_value=_user(role=role)),
    ):
        with pytest.raises(HTTPException) as exc:
            await require_admin(MagicMock(), MagicMock())
    assert exc.value.status_code == 403
    assert exc.value.detail == "관리자 권한이 필요합니다."


@pytest.mark.asyncio
async def test_require_admin_admin_passes():
    admin = _user(role=UserRole.admin)
    with patch(
        "app.auth.admin_router.get_current_user_from_session",
        new=AsyncMock(return_value=admin),
    ):
        result = await require_admin(MagicMock(), MagicMock())
    assert result is admin


# ---- update_user_role endpoints ----


def _override_admin(admin):
    api.dependency_overrides[require_admin] = lambda: admin


def _clear_override():
    api.dependency_overrides.pop(require_admin, None)


def _db_returns(auth_mock_session, user):
    res = MagicMock()
    res.scalar_one_or_none.return_value = user
    auth_mock_session.execute.return_value = res


def test_update_role_self_demote_400(auth_test_client, auth_mock_session):
    admin = _user(uid=1, role=UserRole.admin)
    _override_admin(admin)
    try:
        resp = auth_test_client.put("/admin/users/1/role", json={"role": "viewer"})
    finally:
        _clear_override()
    assert resp.status_code == 400
    assert "자신의 권한" in resp.json()["detail"]
    auth_mock_session.execute.assert_not_called()


def test_update_role_not_found_404(auth_test_client, auth_mock_session):
    admin = _user(uid=1, role=UserRole.admin)
    _db_returns(auth_mock_session, None)
    _override_admin(admin)
    try:
        resp = auth_test_client.put("/admin/users/2/role", json={"role": "trader"})
    finally:
        _clear_override()
    assert resp.status_code == 404
    assert "사용자를 찾을 수 없습니다." in resp.json()["detail"]


def test_update_role_success_side_effects(auth_test_client, auth_mock_session):
    admin = _user(uid=1, role=UserRole.admin)
    target = _user(uid=2, role=UserRole.viewer)
    _db_returns(auth_mock_session, target)
    _override_admin(admin)
    with (
        patch(
            "app.auth.admin_router.revoke_all_refresh_tokens",
            new=AsyncMock(return_value=3),
        ) as revoke,
        patch("app.auth.admin_router.invalidate_user_cache", new=AsyncMock()) as inval,
    ):
        try:
            resp = auth_test_client.put("/admin/users/2/role", json={"role": "trader"})
        finally:
            _clear_override()
    assert resp.status_code == 200
    assert resp.json()["role"] == "trader"
    revoke.assert_awaited_once_with(auth_mock_session, 2)
    inval.assert_awaited_once_with(2)
    auth_mock_session.commit.assert_awaited()


def test_update_role_invalid_enum_422(auth_test_client):
    admin = _user(uid=1, role=UserRole.admin)
    _override_admin(admin)
    try:
        resp = auth_test_client.put("/admin/users/2/role", json={"role": "superuser"})
    finally:
        _clear_override()
    assert resp.status_code == 422


def test_update_role_commit_error_rolls_back_500(auth_test_client, auth_mock_session):
    admin = _user(uid=1, role=UserRole.admin)
    target = _user(uid=2, role=UserRole.viewer)
    _db_returns(auth_mock_session, target)
    auth_mock_session.commit.side_effect = Exception("boom")
    _override_admin(admin)
    with (
        patch(
            "app.auth.admin_router.revoke_all_refresh_tokens",
            new=AsyncMock(return_value=0),
        ),
        patch("app.auth.admin_router.invalidate_user_cache", new=AsyncMock()),
    ):
        try:
            resp = auth_test_client.put("/admin/users/2/role", json={"role": "trader"})
        finally:
            _clear_override()
            auth_mock_session.commit.side_effect = None
    assert resp.status_code == 500
    auth_mock_session.rollback.assert_awaited()


def test_update_role_cache_failure_swallowed_still_200(
    auth_test_client, auth_mock_session
):
    admin = _user(uid=1, role=UserRole.admin)
    target = _user(uid=2, role=UserRole.viewer)
    _db_returns(auth_mock_session, target)
    _override_admin(admin)
    with (
        patch(
            "app.auth.admin_router.revoke_all_refresh_tokens",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.auth.admin_router.invalidate_user_cache",
            new=AsyncMock(side_effect=Exception("redis down")),
        ),
    ):
        try:
            resp = auth_test_client.put("/admin/users/2/role", json={"role": "trader"})
        finally:
            _clear_override()
    assert resp.status_code == 200


# ---- toggle_user_active endpoints ----


def test_toggle_self_400(auth_test_client, auth_mock_session):
    admin = _user(uid=1, role=UserRole.admin)
    _override_admin(admin)
    try:
        resp = auth_test_client.put("/admin/users/1/toggle")
    finally:
        _clear_override()
    assert resp.status_code == 400
    auth_mock_session.execute.assert_not_called()


def test_toggle_not_found_404(auth_test_client, auth_mock_session):
    admin = _user(uid=1, role=UserRole.admin)
    _db_returns(auth_mock_session, None)
    _override_admin(admin)
    try:
        resp = auth_test_client.put("/admin/users/2/toggle")
    finally:
        _clear_override()
    assert resp.status_code == 404
    assert "사용자를 찾을 수 없습니다." in resp.json()["detail"]


def test_toggle_deactivate_blacklists_user(auth_test_client, auth_mock_session):
    admin = _user(uid=1, role=UserRole.admin)
    target = _user(uid=2, role=UserRole.viewer, is_active=True)
    _db_returns(auth_mock_session, target)
    _override_admin(admin)
    bl = MagicMock()
    bl.blacklist_user = AsyncMock()
    bl.remove_from_blacklist = AsyncMock()
    with (
        patch(
            "app.auth.admin_router.revoke_all_refresh_tokens",
            new=AsyncMock(return_value=0),
        ),
        patch("app.auth.admin_router.invalidate_user_cache", new=AsyncMock()),
        patch("app.auth.admin_router.get_session_blacklist", return_value=bl),
    ):
        try:
            resp = auth_test_client.put("/admin/users/2/toggle")
        finally:
            _clear_override()
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    bl.blacklist_user.assert_awaited_once_with(2)
    bl.remove_from_blacklist.assert_not_awaited()


def test_toggle_reactivate_removes_from_blacklist(auth_test_client, auth_mock_session):
    admin = _user(uid=1, role=UserRole.admin)
    target = _user(uid=2, role=UserRole.viewer, is_active=False)
    _db_returns(auth_mock_session, target)
    _override_admin(admin)
    bl = MagicMock()
    bl.blacklist_user = AsyncMock()
    bl.remove_from_blacklist = AsyncMock()
    with (
        patch(
            "app.auth.admin_router.revoke_all_refresh_tokens",
            new=AsyncMock(return_value=0),
        ),
        patch("app.auth.admin_router.invalidate_user_cache", new=AsyncMock()),
        patch("app.auth.admin_router.get_session_blacklist", return_value=bl),
    ):
        try:
            resp = auth_test_client.put("/admin/users/2/toggle")
        finally:
            _clear_override()
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True
    bl.remove_from_blacklist.assert_awaited_once_with(2)
    bl.blacklist_user.assert_not_awaited()


def test_toggle_commit_error_rolls_back_500(auth_test_client, auth_mock_session):
    admin = _user(uid=1, role=UserRole.admin)
    target = _user(uid=2, role=UserRole.viewer, is_active=True)
    _db_returns(auth_mock_session, target)
    auth_mock_session.commit.side_effect = Exception("boom")
    _override_admin(admin)
    with patch(
        "app.auth.admin_router.revoke_all_refresh_tokens", new=AsyncMock(return_value=0)
    ):
        try:
            resp = auth_test_client.put("/admin/users/2/toggle")
        finally:
            _clear_override()
            auth_mock_session.commit.side_effect = None
    assert resp.status_code == 500
    auth_mock_session.rollback.assert_awaited()
