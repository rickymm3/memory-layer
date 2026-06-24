"""Per-request auth context for the SSE/HTTP MCP server.

Tool functions call current_user_id.get() to find who is making the request.
The Starlette middleware sets it from the validated Bearer token.
"""
from __future__ import annotations

import contextvars

current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user_id", default=None
)
