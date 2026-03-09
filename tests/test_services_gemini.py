from unittest.mock import AsyncMock, patch

import pytest


class TestGeminiService:
    @pytest.mark.asyncio
    @patch("redis.asyncio.Redis")
    async def test_model_rate_limiter(self, mock_redis):
        mock_redis_client = AsyncMock()
        mock_redis.from_url.return_value = mock_redis_client
        mock_redis_client.get.return_value = None

        from app.core.model_rate_limiter import ModelRateLimiter

        limiter = ModelRateLimiter()

        result = await limiter.is_model_available("gemini-2.5-pro", "test_key")
        assert result is True
