"""Tests for Paperclip comment posting MCP tool."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.mcp_server.tooling.paperclip_comment import post_paperclip_comment


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_env_vars():
    with patch.dict("os.environ", {}, clear=True):
        result = await post_paperclip_comment("ROB-73", "test comment")
    assert result["success"] is False
    assert "PAPERCLIP_API_URL" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_identifier():
    env = {"PAPERCLIP_API_URL": "http://localhost:3000", "PAPERCLIP_API_KEY": "key"}
    with patch.dict("os.environ", env, clear=True):
        result = await post_paperclip_comment("", "test comment")
    assert result["success"] is False
    assert "required" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_body():
    env = {"PAPERCLIP_API_URL": "http://localhost:3000", "PAPERCLIP_API_KEY": "key"}
    with patch.dict("os.environ", env, clear=True):
        result = await post_paperclip_comment("ROB-73", "")
    assert result["success"] is False
    assert "required" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_comment_with_company_id():
    env = {
        "PAPERCLIP_API_URL": "http://localhost:3000",
        "PAPERCLIP_API_KEY": "test-key",
        "PAPERCLIP_COMPANY_ID": "company-123",
    }

    search_response = httpx.Response(
        200,
        json=[{"id": "issue-uuid", "identifier": "ROB-73"}],
        request=httpx.Request(
            "GET", "http://localhost:3000/api/companies/company-123/issues"
        ),
    )
    comment_response = httpx.Response(
        201,
        json={"id": "comment-uuid"},
        request=httpx.Request(
            "POST", "http://localhost:3000/api/issues/issue-uuid/comments"
        ),
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=search_response)
    mock_client.post = AsyncMock(return_value=comment_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.dict("os.environ", env, clear=True),
        patch(
            "app.mcp_server.tooling.paperclip_comment.httpx.AsyncClient",
            return_value=mock_client,
        ),
    ):
        result = await post_paperclip_comment(
            "ROB-73", "## Fill Record\nBought 10 shares"
        )

    assert result["success"] is True
    assert result["comment_id"] == "comment-uuid"
    assert result["issue_identifier"] == "ROB-73"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_comment_without_company_id():
    env = {
        "PAPERCLIP_API_URL": "http://localhost:3000",
        "PAPERCLIP_API_KEY": "test-key",
    }

    search_response = httpx.Response(
        200,
        json={"id": "issue-uuid", "identifier": "ROB-73"},
        request=httpx.Request(
            "GET", "http://localhost:3000/api/issues/by-identifier/ROB-73"
        ),
    )
    comment_response = httpx.Response(
        201,
        json={"id": "comment-uuid"},
        request=httpx.Request(
            "POST", "http://localhost:3000/api/issues/issue-uuid/comments"
        ),
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=search_response)
    mock_client.post = AsyncMock(return_value=comment_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.dict("os.environ", env, clear=True),
        patch(
            "app.mcp_server.tooling.paperclip_comment.httpx.AsyncClient",
            return_value=mock_client,
        ),
    ):
        result = await post_paperclip_comment("ROB-73", "test comment")

    assert result["success"] is True
    assert result["comment_id"] == "comment-uuid"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_issue_not_found():
    env = {
        "PAPERCLIP_API_URL": "http://localhost:3000",
        "PAPERCLIP_API_KEY": "test-key",
        "PAPERCLIP_COMPANY_ID": "company-123",
    }

    search_response = httpx.Response(
        200,
        json=[{"id": "other-uuid", "identifier": "ROB-99"}],
        request=httpx.Request(
            "GET", "http://localhost:3000/api/companies/company-123/issues"
        ),
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=search_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.dict("os.environ", env, clear=True),
        patch(
            "app.mcp_server.tooling.paperclip_comment.httpx.AsyncClient",
            return_value=mock_client,
        ),
    ):
        result = await post_paperclip_comment("ROB-73", "test comment")

    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_api_error():
    env = {
        "PAPERCLIP_API_URL": "http://localhost:3000",
        "PAPERCLIP_API_KEY": "test-key",
        "PAPERCLIP_COMPANY_ID": "company-123",
    }

    search_response = httpx.Response(
        500,
        json={"error": "Internal server error"},
        request=httpx.Request(
            "GET", "http://localhost:3000/api/companies/company-123/issues"
        ),
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=search_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.dict("os.environ", env, clear=True),
        patch(
            "app.mcp_server.tooling.paperclip_comment.httpx.AsyncClient",
            return_value=mock_client,
        ),
    ):
        result = await post_paperclip_comment("ROB-73", "test comment")

    assert result["success"] is False
    assert "HTTP 500" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_comment_post_failure():
    env = {
        "PAPERCLIP_API_URL": "http://localhost:3000",
        "PAPERCLIP_API_KEY": "test-key",
        "PAPERCLIP_COMPANY_ID": "company-123",
    }

    search_response = httpx.Response(
        200,
        json=[{"id": "issue-uuid", "identifier": "ROB-73"}],
        request=httpx.Request(
            "GET", "http://localhost:3000/api/companies/company-123/issues"
        ),
    )
    comment_response = httpx.Response(
        403,
        json={"error": "Forbidden"},
        request=httpx.Request(
            "POST", "http://localhost:3000/api/issues/issue-uuid/comments"
        ),
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=search_response)
    mock_client.post = AsyncMock(return_value=comment_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.dict("os.environ", env, clear=True),
        patch(
            "app.mcp_server.tooling.paperclip_comment.httpx.AsyncClient",
            return_value=mock_client,
        ),
    ):
        result = await post_paperclip_comment("ROB-73", "test comment")

    assert result["success"] is False
    assert "HTTP 403" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_timeout_handling():
    env = {
        "PAPERCLIP_API_URL": "http://localhost:3000",
        "PAPERCLIP_API_KEY": "test-key",
        "PAPERCLIP_COMPANY_ID": "company-123",
    }

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.dict("os.environ", env, clear=True),
        patch(
            "app.mcp_server.tooling.paperclip_comment.httpx.AsyncClient",
            return_value=mock_client,
        ),
    ):
        result = await post_paperclip_comment("ROB-73", "test comment")

    assert result["success"] is False
    assert "timed out" in result["error"]
