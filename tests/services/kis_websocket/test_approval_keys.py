from unittest.mock import AsyncMock

import pytest

from app.services import kis_websocket as mod


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
    mocker.patch.object(mod, "_get_cached_approval_key", new=AsyncMock(return_value="cached"))
    # We also need to patch _is_valid_approval_key if it's used inside
    # but based on the code it's used to check the cached value.
    
    result = await mod.get_approval_key()
    assert result == "cached"


@pytest.mark.asyncio
async def test_get_approval_key_miss_flow(mocker):
    """Pin the flow when cache is empty"""
    mocker.patch.object(mod, "_get_cached_approval_key", new=AsyncMock(return_value=None))
    mock_issue = mocker.patch.object(mod, "_issue_approval_key", new=AsyncMock(return_value="fresh"))
    mock_cache = mocker.patch.object(mod, "_cache_approval_key", new=AsyncMock())
    
    result = await mod.get_approval_key()
    
    assert result == "fresh"
    mock_issue.assert_awaited_once()
    mock_cache.assert_awaited_once_with("fresh")


@pytest.mark.asyncio
async def test_close_approval_key_redis_idempotency(mocker):
    """Pin that close_approval_key_redis is idempotent and clears the global client"""
    mock_redis = AsyncMock()
    mocker.patch.object(mod, "_redis_client", mock_redis)
    
    await mod.close_approval_key_redis()
    
    assert mock_redis.close.call_count == 1
    assert mod._redis_client is None
    
    # Second call should be safe and not call close again on the same object
    await mod.close_approval_key_redis()
    assert mock_redis.close.call_count == 1
