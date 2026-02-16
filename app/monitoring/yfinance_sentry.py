from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import sentry_sdk
from curl_cffi.requests import Session


def _extract_span_path(url: Any) -> str:
    if not isinstance(url, str):
        return "/unknown"
    try:
        path = urlsplit(url).path
    except (TypeError, ValueError, AttributeError):
        return "/unknown"
    return path or "/unknown"


class SentryTracingCurlSession(Session):
    def request(self, method: str, url: str, **kwargs):  # type: ignore[override]
        method_up = str(method).upper()
        path = _extract_span_path(url)

        with sentry_sdk.start_span(
            op="http.client", name=f"{method_up} {path}"
        ) as span:
            span.set_data("url", url)
            span.set_data("http.request.method", method_up)
            response = super().request(method, url, **kwargs)
            span.set_data("http.response.status_code", response.status_code)
            return response


def build_yfinance_tracing_session() -> Session:
    return SentryTracingCurlSession()


__all__ = ["SentryTracingCurlSession", "build_yfinance_tracing_session"]
