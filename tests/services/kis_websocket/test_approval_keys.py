from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import kis_websocket as mod
from app.services.kis_websocket_internal.constants import (
    APPROVAL_KEY_CACHE_KEY,
    APPROVAL_KEY_TTL_SECONDS,
)

# We need to patch the internal module because the facade only re-exports
INTERNAL = "app.services.kis_websocket_internal.approval_keys"


@pytest.mark.asyncio
async def test_is_valid_approval_key_semantics():
    """Pin the semantics of _is_valid_approval_key"""
    assert mod._is_valid_approval_key(None) is False
    assert mod._is_valid_approval_key("") is False
    assert mod._is_valid_approval_key("   ") is False
    assert mod._is_valid_approval_key("valid") is True
    assert mod._is_valid_approval_key("  valid  ") is True


@pytest.mark.asyncio
async def test_get_approval_key_uses_cached_value(mocker):
    """Pin that get_approval_key uses cached value if valid"""
    mocker.patch(f"{INTERNAL}._get_cached_approval_key", new=AsyncMock(return_value="cached"))
    
    result = await mod.get_approval_key()
    assert result == "cached"


@pytest.mark.asyncio
async def test_get_approval_key_miss_flow(mocker):
    """Pin the flow when cache is empty"""
    mocker.patch(f"{INTERNAL}._get_cached_approval_key", new=AsyncMock(return_value=None))
    mock_issue = mocker.patch(f"{INTERNAL}._issue_approval_key", new=AsyncMock(return_value="fresh"))
    mock_cache = mocker.patch(f"{INTERNAL}._cache_approval_key", new=AsyncMock())
    
    result = await mod.get_approval_key()
    
    assert result == "fresh"
    mock_issue.assert_awaited_once()
    mock_cache.assert_awaited_once_with("fresh")


@pytest.mark.asyncio
async def test_close_approval_key_redis_idempotency(mocker):
    """Pin that close_approval_key_redis is idempotent and clears the global client"""
    mock_redis = AsyncMock()
    mocker.patch(f"{INTERNAL}._redis_client", mock_redis)
    
    await mod.close_approval_key_redis()
    
    assert mock_redis.close.call_count == 1
    # Check if the global variable in the internal module is cleared
    import app.services.kis_websocket_internal.approval_keys as internal_mod
    assert internal_mod._redis_client is None
    
    # Second call should be safe and not call close again on the same object
    await mod.close_approval_key_redis()
    assert mock_redis.close.call_count == 1


@pytest.mark.unit
class TestKISWebSocketApprovalKey:
    """Tests for Approval Key issuance (from monolith)"""

    @pytest.mark.asyncio
    async def test_issue_approval_key_success(self):
        """Approval Key 발급 성공 케이스"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"approval_key": "test_approval_key"}
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)

        with patch(f"{INTERNAL}.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch(
                f"{INTERNAL}._get_cached_approval_key",
                return_value=None,
            ):
                with patch(
                    f"{INTERNAL}._cache_approval_key",
                    return_value=None,
                ):
                    approval_key = await mod.get_approval_key()

                    assert approval_key == "test_approval_key"

    @pytest.mark.asyncio
    async def test_issue_approval_key_missing_key(self):
        """Approval Key 응답에 키 없음 실패 케이스"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"error": "unauthorized"}
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)

        with patch(f"{INTERNAL}.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch(
                f"{INTERNAL}._get_cached_approval_key",
                return_value=None,
            ):
                with patch(
                    f"{INTERNAL}._cache_approval_key",
                    return_value=None,
                ):
                    with pytest.raises(Exception, match="Approval Key not found"):
                        await mod.get_approval_key()


@pytest.mark.unit
class TestApprovalKeyRedisCache:
    """Tests for Approval Key Redis caching (from monolith)"""

    @pytest.mark.asyncio
    async def test_get_cached_approval_key_hit(self):
        """Redis GET 성공 시 캐시된 키 반환"""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="cached_approval_key_123")

        with patch(
            f"{INTERNAL}._get_redis_client",
            return_value=mock_redis,
        ):
            result = await mod._get_cached_approval_key()

            assert result == "cached_approval_key_123"
            mock_redis.get.assert_called_once_with(APPROVAL_KEY_CACHE_KEY)

    @pytest.mark.asyncio
    async def test_get_cached_approval_key_miss(self):
        """Redis GET 빈값(None) 시 None 반환"""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)

        with patch(
            f"{INTERNAL}._get_redis_client",
            return_value=mock_redis,
        ):
            result = await mod._get_cached_approval_key()

            assert result is None
            mock_redis.get.assert_called_once_with(APPROVAL_KEY_CACHE_KEY)

    @pytest.mark.asyncio
    async def test_get_cached_approval_key_redis_error_propagates(self):
        """Redis 예외 발생 시 전파 (엄격 실패 정책)"""
        from redis.asyncio import RedisError

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=RedisError("Connection refused"))

        with patch(
            f"{INTERNAL}._get_redis_client",
            return_value=mock_redis,
        ):
            with pytest.raises(RedisError, match="Connection refused"):
                await mod._get_cached_approval_key()

    @pytest.mark.asyncio
    async def test_cache_approval_key_sets_with_ttl(self):
        """Redis SET 호출 시 23시간 TTL 적용"""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)

        with patch(
            f"{INTERNAL}._get_redis_client",
            return_value=mock_redis,
        ):
            await mod._cache_approval_key("new_approval_key_456")

            mock_redis.set.assert_called_once_with(
                APPROVAL_KEY_CACHE_KEY,
                "new_approval_key_456",
                ex=APPROVAL_KEY_TTL_SECONDS,
            )

    @pytest.mark.asyncio
    async def test_cache_approval_key_redis_error_propagates(self):
        """Redis SET 예외 발생 시 전파 (엄격 실패 정책)"""
        from redis.asyncio import RedisError

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=RedisError("Write failed"))

        with patch(
            f"{INTERNAL}._get_redis_client",
            return_value=mock_redis,
        ):
            with pytest.raises(RedisError, match="Write failed"):
                await mod._cache_approval_key("new_key")

    @pytest.mark.asyncio
    async def test_get_approval_key_uses_cached_value(self):
        """캐시 히트 시 재발급 없이 캐시 값 반환"""
        with patch(
            f"{INTERNAL}._get_cached_approval_key",
            return_value="cached_key_789",
        ):
            result = await mod.get_approval_key()

            assert result == "cached_key_789"

    @pytest.mark.asyncio
    async def test_get_approval_key_issues_and_caches_on_miss(self):
        """캐시 미스 시 새로 발급하고 캐시에 저장"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"approval_key": "fresh_key_abc"}
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)

        cache_spy = AsyncMock()

        with patch(f"{INTERNAL}.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch(
                f"{INTERNAL}._get_cached_approval_key",
                return_value=None,
            ):
                with patch(
                    f"{INTERNAL}._cache_approval_key",
                    cache_spy,
                ):
                    result = await mod.get_approval_key()

                    assert result == "fresh_key_abc"
                    cache_spy.assert_called_once_with("fresh_key_abc")

    @pytest.mark.asyncio
    async def test_cache_constants_are_correct(self):
        """캐시 상수값 검증"""
        assert APPROVAL_KEY_CACHE_KEY == "kis:websocket:approval_key"
        assert APPROVAL_KEY_TTL_SECONDS == 82800  # 23시간


@pytest.mark.unit
class TestApprovalKeyValidation:
    """Tests for Approval Key validation helper (from monolith)"""

    def test_valid_key_returns_true(self):
        """유효한 키는 True 반환"""
        assert mod._is_valid_approval_key("valid_key_123") is True

    def test_none_returns_false(self):
        """None은 False 반환"""
        assert mod._is_valid_approval_key(None) is False

    def test_empty_string_returns_false(self):
        """빈 문자열은 False 반환"""
        assert mod._is_valid_approval_key("") is False

    def test_whitespace_only_returns_false(self):
        """공백만 있는 문자열은 False 반환"""
        assert mod._is_valid_approval_key("   ") is False
        assert mod._is_valid_approval_key("\t\n") is False

    def test_key_with_surrounding_whitespace_is_valid(self):
        """앞뒤 공백이 있는 키는 유효"""
        assert mod._is_valid_approval_key("  valid_key  ") is True


@pytest.mark.unit
class TestApprovalKeyEmptyCacheMiss:
    """Tests for empty/whitespace cache values being treated as cache miss (from monolith)"""

    @pytest.mark.asyncio
    async def test_empty_string_cache_treated_as_miss(self):
        """빈 문자열 캐시값은 미스로 처리되어 재발급"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"approval_key": "fresh_key_empty"}
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)

        cache_spy = AsyncMock()

        with patch(f"{INTERNAL}.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch(
                f"{INTERNAL}._get_cached_approval_key",
                return_value="",  # Empty string from cache
            ):
                with patch(
                    f"{INTERNAL}._cache_approval_key",
                    cache_spy,
                ):
                    result = await mod.get_approval_key()

                    assert result == "fresh_key_empty"
                    cache_spy.assert_called_once_with("fresh_key_empty")

    @pytest.mark.asyncio
    async def test_whitespace_cache_treated_as_miss(self):
        """공백 캐시값은 미스로 처리되어 재발급"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"approval_key": "fresh_key_ws"}
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)

        cache_spy = AsyncMock()

        with patch(f"{INTERNAL}.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch(
                f"{INTERNAL}._get_cached_approval_key",
                return_value="   ",  # Whitespace from cache
            ):
                with patch(
                    f"{INTERNAL}._cache_approval_key",
                    cache_spy,
                ):
                    result = await mod.get_approval_key()

                    assert result == "fresh_key_ws"
                    cache_spy.assert_called_once_with("fresh_key_ws")


@pytest.mark.unit
class TestApprovalKeyCacheHitNoReissue:
    """Tests for cache hit blocking re-issuance (from monolith)"""

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_call_issue_or_cache(self):
        """캐시 히트 시 _issue_approval_key와 _cache_approval_key 호출되지 않음"""
        issue_spy = AsyncMock(return_value="should_not_be_called")
        cache_spy = AsyncMock()

        with patch(
            f"{INTERNAL}._get_cached_approval_key",
            return_value="cached_valid_key",
        ):
            with patch(
                f"{INTERNAL}._issue_approval_key",
                issue_spy,
            ):
                with patch(
                    f"{INTERNAL}._cache_approval_key",
                    cache_spy,
                ):
                    result = await mod.get_approval_key()

                    assert result == "cached_valid_key"
                    issue_spy.assert_not_called()
                    cache_spy.assert_not_called()


@pytest.mark.unit
class TestCloseApprovalKeyRedis:
    """Tests for Redis client cleanup function (from monolith)"""

    @pytest.mark.asyncio
    async def test_close_existing_client(self):
        """기존 클라이언트 존재 시 close 호출"""
        import app.services.kis_websocket_internal.approval_keys as internal_mod

        mock_redis = AsyncMock()
        mock_redis.close = AsyncMock()
        internal_mod._redis_client = mock_redis

        await mod.close_approval_key_redis()

        mock_redis.close.assert_called_once()
        assert internal_mod._redis_client is None

    @pytest.mark.asyncio
    async def test_close_no_client_is_idempotent(self):
        """클라이언트 없을 때 호출해도 예외 없음 (idempotent)"""
        import app.services.kis_websocket_internal.approval_keys as internal_mod

        internal_mod._redis_client = None

        # Should not raise
        await mod.close_approval_key_redis()

        assert internal_mod._redis_client is None

    @pytest.mark.asyncio
    async def test_close_multiple_times_is_idempotent(self):
        """여러 번 호출해도 안전 (idempotent)"""
        import app.services.kis_websocket_internal.approval_keys as internal_mod

        mock_redis = AsyncMock()
        mock_redis.close = AsyncMock()
        internal_mod._redis_client = mock_redis

        await mod.close_approval_key_redis()
        assert mock_redis.close.call_count == 1

        # Second call should be safe
        await mod.close_approval_key_redis()
        assert mock_redis.close.call_count == 1  # Not called again

        assert internal_mod._redis_client is None
