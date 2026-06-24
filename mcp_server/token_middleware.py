"""Starlette ASGI middleware: validates Bearer token → injects source_user_id context.

On every HTTP request:
  1. Reads Authorization: Bearer <token> header
  2. Looks up token in users table (parameterized query, no f-string injection)
  3. Sets current_user_id ContextVar for the duration of the request
  4. Returns 401 if token is missing or invalid

Secrets never appear in responses.
"""
from __future__ import annotations

import json
import os

import psycopg

from mcp_server.auth_context import current_user_id


def _resolve_token(token: str) -> str | None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return None
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT username FROM users WHERE api_token = %s AND is_active = true LIMIT 1;",
                    (token,),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


class TokenAuthMiddleware:
    """ASGI middleware: validates Bearer token and sets current_user_id context."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Extract Bearer token from headers
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")

        if not auth.startswith("Bearer "):
            await self._reject(send, 401, "Authorization: Bearer <api_token> required")
            return

        token = auth[7:].strip()
        username = _resolve_token(token)

        if not username:
            await self._reject(send, 401, "Invalid or inactive api_token")
            return

        # Set context for this request's async task
        token_var = current_user_id.set(username)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user_id.reset(token_var)

    @staticmethod
    async def _reject(send, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        })
        await send({"type": "http.response.body", "body": body})
