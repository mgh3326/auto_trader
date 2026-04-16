"""Post comments to Paperclip issues via the Paperclip API."""

from __future__ import annotations

import os
from typing import Any

import httpx


async def post_paperclip_comment(
    issue_identifier: str,
    body: str,
) -> dict[str, Any]:
    """Post a markdown comment to a Paperclip issue.

    Args:
        issue_identifier: Paperclip issue identifier (e.g. "ROB-73").
        body: Markdown comment body.

    Returns:
        Dict with success status and comment ID or error message.
    """
    api_url = os.environ.get("PAPERCLIP_API_URL", "").rstrip("/")
    api_key = os.environ.get("PAPERCLIP_API_KEY", "")

    if not api_url or not api_key:
        return {
            "success": False,
            "error": "PAPERCLIP_API_URL and PAPERCLIP_API_KEY must be set",
        }

    if not issue_identifier or not body:
        return {
            "success": False,
            "error": "issue_identifier and body are required",
        }

    company_id = os.environ.get("PAPERCLIP_COMPANY_ID", "")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if company_id:
                search_resp = await client.get(
                    f"{api_url}/api/companies/{company_id}/issues",
                    params={
                        "q": issue_identifier,
                        "status": "todo,in_progress,in_review,blocked",
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            else:
                search_resp = await client.get(
                    f"{api_url}/api/issues/by-identifier/{issue_identifier}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )

            if search_resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"Failed to find issue {issue_identifier}: HTTP {search_resp.status_code}",
                }

            data = search_resp.json()

            if company_id and isinstance(data, list):
                issue = next(
                    (i for i in data if i.get("identifier") == issue_identifier),
                    None,
                )
                if not issue:
                    return {
                        "success": False,
                        "error": f"Issue {issue_identifier} not found",
                    }
                issue_id = issue["id"]
            elif isinstance(data, dict) and data.get("id"):
                issue_id = data["id"]
            else:
                return {
                    "success": False,
                    "error": f"Issue {issue_identifier} not found",
                }

            comment_resp = await client.post(
                f"{api_url}/api/issues/{issue_id}/comments",
                json={"body": body},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

            if comment_resp.status_code not in (200, 201):
                return {
                    "success": False,
                    "error": f"Failed to post comment: HTTP {comment_resp.status_code}",
                }

            result = comment_resp.json()
            return {
                "success": True,
                "comment_id": result.get("id"),
                "issue_identifier": issue_identifier,
            }

    except httpx.TimeoutException:
        return {"success": False, "error": "Request timed out"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


__all__ = ["post_paperclip_comment"]
