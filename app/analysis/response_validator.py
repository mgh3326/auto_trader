from __future__ import annotations

import json

from app.analysis.models import StockAnalysisResponse


class ResponseValidator:
    def validate(
        self,
        response_text: str,
        *,
        use_json: bool,
    ) -> str | StockAnalysisResponse:
        if not use_json:
            return response_text

        parsed_response = json.loads(response_text)
        return StockAnalysisResponse(**parsed_response)
