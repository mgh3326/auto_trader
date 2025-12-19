"""
Tests for Redis Token Manager.
"""

import json
import time
from unittest.mock import AsyncMock, patch

import pytest


class TestRedisTokenManagerLock:
    """Test Redis Token Manager lock functionality."""

    @pytest.mark.asyncio
    async def test_acquire_lock_success(self):
        """Test successful lock acquisition."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()
        mock_redis.set.return_value = True
        mock_redis.get.return_value = None  # Will be set by the test

        with patch.object(manager, "_get_redis_client", return_value=mock_redis):
            # Mock get to return the same value that was set
            async def mock_set(key, value, nx=False, ex=None):
                mock_redis.get.return_value = value
                return True

            mock_redis.set.side_effect = mock_set

            result = await manager._acquire_lock()

            assert result is True
            assert manager._current_lock_value is not None
            mock_redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_acquire_lock_failure(self):
        """Test lock acquisition failure when already locked."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()
        mock_redis.set.return_value = False  # Lock already exists

        with patch.object(manager, "_get_redis_client", return_value=mock_redis):
            result = await manager._acquire_lock()

            assert result is False
            assert manager._current_lock_value is None

    @pytest.mark.asyncio
    async def test_release_lock_uses_stored_value(self):
        """Test that release_lock uses the stored lock value, not a new one."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()
        mock_redis.eval.return_value = 1

        # Simulate having acquired a lock
        stored_lock_value = "1234567890.123:12345:6789"
        manager._current_lock_value = stored_lock_value

        with patch.object(manager, "_get_redis_client", return_value=mock_redis):
            await manager._release_lock()

            # Verify eval was called with the stored lock value
            mock_redis.eval.assert_called_once()
            call_args = mock_redis.eval.call_args
            assert call_args[0][3] == stored_lock_value  # 4th argument is lock value

            # Verify lock value is cleared after release
            assert manager._current_lock_value is None

    @pytest.mark.asyncio
    async def test_release_lock_without_stored_value(self):
        """Test release_lock does nothing when no lock value is stored."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()

        # No lock value stored
        manager._current_lock_value = None

        with patch.object(manager, "_get_redis_client", return_value=mock_redis):
            await manager._release_lock()

            # Verify eval was NOT called
            mock_redis.eval.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_value_consistency(self):
        """Test that acquire and release use the same lock value."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()
        mock_redis.eval.return_value = 1

        captured_lock_values = []

        async def mock_set(key, value, nx=False, ex=None):
            captured_lock_values.append(("set", value))
            mock_redis.get.return_value = value
            return True

        mock_redis.set.side_effect = mock_set

        with patch.object(manager, "_get_redis_client", return_value=mock_redis):
            # Acquire lock
            await manager._acquire_lock()
            acquire_value = manager._current_lock_value

            # Release lock
            await manager._release_lock()

            # Verify the same value was used
            call_args = mock_redis.eval.call_args
            release_value = call_args[0][3]

            assert acquire_value == release_value


class TestRedisTokenManagerToken:
    """Test Redis Token Manager token operations."""

    @pytest.mark.asyncio
    async def test_get_token_valid(self):
        """Test getting a valid token from Redis."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()

        # Token that expires in 2 hours
        token_data = {
            "access_token": "test_token_123",
            "expires_at": time.time() + 7200,
            "created_at": time.time(),
        }
        mock_redis.get.return_value = json.dumps(token_data)

        with patch.object(manager, "_get_redis_client", return_value=mock_redis):
            result = await manager.get_token()

            assert result == "test_token_123"

    @pytest.mark.asyncio
    async def test_get_token_expired(self):
        """Test getting an expired token returns None."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()

        # Token that expired 1 hour ago
        token_data = {
            "access_token": "expired_token",
            "expires_at": time.time() - 3600,
            "created_at": time.time() - 7200,
        }
        mock_redis.get.return_value = json.dumps(token_data)

        with patch.object(manager, "_get_redis_client", return_value=mock_redis):
            result = await manager.get_token()

            assert result is None

    @pytest.mark.asyncio
    async def test_get_token_not_exists(self):
        """Test getting token when none exists."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None

        with patch.object(manager, "_get_redis_client", return_value=mock_redis):
            result = await manager.get_token()

            assert result is None

    @pytest.mark.asyncio
    async def test_save_token(self):
        """Test saving a token to Redis."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()

        with patch.object(manager, "_get_redis_client", return_value=mock_redis):
            await manager.save_token("new_token_456", expires_in=3600)

            mock_redis.set.assert_called_once()
            call_args = mock_redis.set.call_args

            # Verify the key
            assert call_args[0][0] == "kis:access_token"

            # Verify the token data
            saved_data = json.loads(call_args[0][1])
            assert saved_data["access_token"] == "new_token_456"
            assert "expires_at" in saved_data
            assert "created_at" in saved_data

    @pytest.mark.asyncio
    async def test_clear_token(self):
        """Test clearing token from Redis."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()

        with patch.object(manager, "_get_redis_client", return_value=mock_redis):
            await manager.clear_token()

            mock_redis.delete.assert_called_once_with("kis:access_token")


class TestRefreshTokenWithLock:
    """Test the refresh_token_with_lock functionality."""

    @pytest.mark.asyncio
    async def test_returns_existing_token(self):
        """Test that existing valid token is returned without lock."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()

        with patch.object(manager, "get_token", return_value="existing_token"):
            token_fetcher = AsyncMock()

            result = await manager.refresh_token_with_lock(token_fetcher)

            assert result == "existing_token"
            token_fetcher.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetches_new_token_with_lock(self):
        """Test that new token is fetched when none exists."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()
        mock_redis = AsyncMock()

        # Simulate no existing token, then lock acquired, then token saved
        get_token_calls = [None, None, None, None]  # Multiple None for retry checks
        get_token_index = 0

        async def mock_get_token():
            nonlocal get_token_index
            if get_token_index < len(get_token_calls):
                result = get_token_calls[get_token_index]
                get_token_index += 1
                return result
            return None

        async def mock_set(key, value, nx=False, ex=None):
            mock_redis.get.return_value = value
            return True

        mock_redis.set.side_effect = mock_set
        mock_redis.eval.return_value = 1

        token_fetcher = AsyncMock(return_value=("new_access_token", 3600))

        with (
            patch.object(manager, "get_token", side_effect=mock_get_token),
            patch.object(manager, "_get_redis_client", return_value=mock_redis),
            patch.object(manager, "save_token", new_callable=AsyncMock) as mock_save,
        ):
            result = await manager.refresh_token_with_lock(token_fetcher)

            assert result == "new_access_token"
            token_fetcher.assert_called_once()
            mock_save.assert_called_once_with("new_access_token", 3600)

    @pytest.mark.asyncio
    async def test_lock_acquisition_failure_raises_error(self):
        """Test that RuntimeError is raised when lock cannot be acquired."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()

        # Always return None for get_token (no existing token)
        # Always fail to acquire lock
        with (
            patch.object(manager, "get_token", return_value=None),
            patch.object(manager, "_acquire_lock", return_value=False),
        ):
            token_fetcher = AsyncMock()

            with pytest.raises(RuntimeError, match="토큰 발급 락 획득 실패"):
                await manager.refresh_token_with_lock(token_fetcher)

    @pytest.mark.asyncio
    async def test_lock_released_after_success(self):
        """Test that lock is released after successful token fetch."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()

        get_token_returns = [
            None,
            None,
            None,
            None,
        ]  # For initial checks and post-lock check
        get_token_index = 0

        async def mock_get_token():
            nonlocal get_token_index
            if get_token_index < len(get_token_returns):
                result = get_token_returns[get_token_index]
                get_token_index += 1
                return result
            return None

        token_fetcher = AsyncMock(return_value=("token", 3600))

        with (
            patch.object(manager, "get_token", side_effect=mock_get_token),
            patch.object(manager, "_acquire_lock", return_value=True),
            patch.object(
                manager, "_release_lock", new_callable=AsyncMock
            ) as mock_release,
            patch.object(manager, "save_token", new_callable=AsyncMock),
        ):
            manager._current_lock_value = "test_lock_value"  # Simulate lock acquired
            await manager.refresh_token_with_lock(token_fetcher)

            mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_lock_released_after_failure(self):
        """Test that lock is released even when token fetch fails."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()

        get_token_returns = [None, None, None, None]
        get_token_index = 0

        async def mock_get_token():
            nonlocal get_token_index
            if get_token_index < len(get_token_returns):
                result = get_token_returns[get_token_index]
                get_token_index += 1
                return result
            return None

        token_fetcher = AsyncMock(side_effect=Exception("API Error"))

        with (
            patch.object(manager, "get_token", side_effect=mock_get_token),
            patch.object(manager, "_acquire_lock", return_value=True),
            patch.object(
                manager, "_release_lock", new_callable=AsyncMock
            ) as mock_release,
        ):
            manager._current_lock_value = "test_lock_value"

            with pytest.raises(Exception, match="API Error"):
                await manager.refresh_token_with_lock(token_fetcher)

            mock_release.assert_called_once()


class TestTokenValidity:
    """Test token validity checking."""

    def test_is_token_valid_with_valid_token(self):
        """Test valid token is recognized."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()

        token_data = {
            "access_token": "valid_token",
            "expires_at": time.time() + 3600,  # Expires in 1 hour
        }

        assert manager._is_token_valid(token_data) is True

    def test_is_token_valid_with_expired_token(self):
        """Test expired token is rejected."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()

        token_data = {
            "access_token": "expired_token",
            "expires_at": time.time() - 3600,  # Expired 1 hour ago
        }

        assert manager._is_token_valid(token_data) is False

    def test_is_token_valid_within_buffer(self):
        """Test token within expiry buffer is considered expired."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()

        # Token expires in 30 seconds, but buffer is 60 seconds
        token_data = {
            "access_token": "soon_expired",
            "expires_at": time.time() + 30,
        }

        assert manager._is_token_valid(token_data) is False

    def test_is_token_valid_with_empty_data(self):
        """Test empty token data is rejected."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()

        assert manager._is_token_valid({}) is False
        assert manager._is_token_valid(None) is False

    def test_is_token_valid_without_expires_at(self):
        """Test token data without expires_at is rejected."""
        from app.services.redis_token_manager import RedisTokenManager

        manager = RedisTokenManager()

        token_data = {"access_token": "token_without_expiry"}

        assert manager._is_token_valid(token_data) is False
