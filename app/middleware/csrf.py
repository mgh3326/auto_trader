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
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. Setup CSRF token in state for templates
        state = scope.setdefault("state", {})
        request = Request(scope)
        # We get the token from cookie or generate a new one
        csrf_cookie = request.cookies.get(self.cookie_name)
        if csrf_cookie:
            csrf_token = csrf_cookie
        else:
            # Generate a new token if not present
            csrf_token = cast(str, self.serializer.dumps(secrets.token_urlsafe(128)))
        
        state["csrftoken"] = csrf_token

        # 2. To support reading the form body, we need to wrap the receive channel
        # because starlette-csrf's __call__ creates a Request without receive,
        # and we need to consume the body to check for the csrf token.
        
        # We only do this for non-safe methods that might have a form body
        if request.method not in self.safe_methods and not self._url_is_exempt(request.url):
            body = b""
            more_body = True
            messages = []
            
            # Read the entire body
            while more_body:
                message = await receive()
                messages.append(message)
                body += message.get("body", b"")
                more_body = message.get("more_body", False)
            
            # Create a new receive channel that replays the messages
            async def replaying_receive() -> Message:
                if messages:
                    return messages.pop(0)
                return await receive()

            # Store the body so _get_submitted_csrf_token can access it via a temporary Request
            scope["_csrf_body"] = body
            # Update receive for the rest of the chain
            receive = replaying_receive

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
            # Use the body we captured in __call__
            body = request.scope.get("_csrf_body", b"")
            if not body:
                return None
            
            from starlette.formparsers import FormParser
            
            async def body_stream():
                yield body
                yield b""
                
            parser = FormParser(request.headers, body_stream())
            form = await parser.parse()
            token = form.get(self.cookie_name)
            return token if isinstance(token, str) else None

        return None
