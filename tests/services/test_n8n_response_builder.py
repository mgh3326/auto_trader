"""Unit tests for app.services.n8n_response_builder."""

from __future__ import annotations

import json

from pydantic import BaseModel

# Import will FAIL until Task 2 creates the module.
from app.services.n8n_response_builder import n8n_error_response


class _SampleResponse(BaseModel):
    success: bool
    errors: list[dict]


class TestN8nErrorResponse:
    def test_returns_500_status_code(self) -> None:
        payload = _SampleResponse(success=False, errors=[{"error": "boom"}])
        response = n8n_error_response(payload)
        assert response.status_code == 500

    def test_content_matches_model_dump(self) -> None:
        payload = _SampleResponse(success=False, errors=[{"error": "boom"}])
        response = n8n_error_response(payload)
        body = json.loads(response.body)
        assert body == {"success": False, "errors": [{"error": "boom"}]}

    def test_success_false_preserved(self) -> None:
        payload = _SampleResponse(success=False, errors=[])
        response = n8n_error_response(payload)
        body = json.loads(response.body)
        assert body["success"] is False

    def test_nested_errors_list_preserved(self) -> None:
        payload = _SampleResponse(
            success=False,
            errors=[{"error": "first"}, {"market": "kr", "error": "second"}],
        )
        response = n8n_error_response(payload)
        body = json.loads(response.body)
        assert len(body["errors"]) == 2
        assert body["errors"][1]["market"] == "kr"
