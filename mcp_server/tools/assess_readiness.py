from __future__ import annotations

from typing import Any

from mcp_server.tools.task_context import get_task_context
from app.task_readiness import assess_task_readiness


def assess_task_readiness_handler(
    task_description: str,
    project_scope: str,
    model_scope: str | None = None,
    task_hint: str | None = None,
    user_requested_web: bool = False,
) -> dict[str, Any]:
    """Fetch task context then evaluate readiness deterministically.

    Calls get_task_context internally (single DB round-trip) then applies
    assess_task_readiness rules. Returns the readiness verdict plus a
    task_context_summary with counts for diagnostic use.
    """
    task_context = get_task_context(
        project_scope=project_scope,
        model_scope=model_scope,
        task_hint=task_hint,
        recent_tasks=3,
    )
    readiness = assess_task_readiness(
        task_description=task_description,
        project_scope=project_scope,
        model_scope=model_scope,
        task_context=task_context,
        user_requested_web=user_requested_web,
        task_hint=task_hint,
    )
    return {**readiness, "task_context_summary": task_context.get("summary", {})}
