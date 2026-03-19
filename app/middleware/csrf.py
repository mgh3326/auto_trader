from __future__ import annotations

import http.cookies
import secrets
from typing import Optional, cast

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send
from starlette_csrf import CSRFMiddleware


class TemplateFormCSRFMiddleware(CSRFMiddleware):
    """`starlette-csrf` adapter for Jinja forms and hidden-field POSTs."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            state = scope.setdefault("state", {})
            request = Request(scope)
            state["csrftoken"] = request.cookies.get(self.cookie_name) or cast(
                str, self.serializer.dumps(secrets.token_urlsafe(128))
            )

        await super().__call__(scope, receive, send)

    async def send(self, message: Message, send: Send, scope: Scope) -> None:
        request = Request(scope)
        csrf_cookie = request.cookies.get(self.cookie_name)
        csrf_token = scope.get("state", {}).get("csrftoken")

        if csrf_cookie is None and csrf_token and message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            cookie: http.cookies.BaseCookie = http.cookies.SimpleCookie()
            cookie[self.cookie_name] = csrf_token
            cookie[self.cookie_name]["path"] = self.cookie_path
            cookie[self.cookie_name]["secure"] = self.cookie_secure
            cookie[self.cookie_name]["httponly"] = self.cookie_httponly
            cookie[self.cookie_name]["samesite"] = self.cookie_samesite
            if self.cookie_domain is not None:
                cookie[self.cookie_name]["domain"] = self.cookie_domain
            headers.append("set-cookie", cookie.output(header="").strip())

        await send(message)

    async def _get_submitted_csrf_token(self, request: Request) -> Optional[str]:
        header_token = request.headers.get(self.header_name)
        if header_token:
            return header_token

        content_type = request.headers.get("content-type", "")
        if (
            "application/x-www-form-urlencoded" in content_type
            or "multipart/form-data" in content_type
        ):
            form = await request.form()
            token = form.get(self.cookie_name)
            return token if isinstance(token, str) else None

        return None
