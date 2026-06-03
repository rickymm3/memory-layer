#!/usr/bin/env python
"""CLI: assess task readiness against stored memory context.

Usage:
    python scripts/assess_task.py \\
        --scope project:memory-layer \\
        --task "implement OAuth login handler" \\
        [--model model:qwen3-8b] \\
        [--hint "OAuth auth login implementation"] \\
        [--web]

Exit code: 0 if ready, 1 if not ready or on error.
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, ".")

from mcp_server.tools.task_context import get_task_context
from app.task_readiness import assess_task_readiness


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assess task readiness from stored memory context."
    )
    parser.add_argument(
        "--scope", required=True, help="Project scope (e.g. project:memory-layer)"
    )
    parser.add_argument("--task", required=True, help="Task description")
    parser.add_argument(
        "--model", default=None, help="Model scope (e.g. model:qwen3-8b)"
    )
    parser.add_argument(
        "--hint", default=None, help="Optional task hint for semantic search"
    )
    parser.add_argument(
        "--web", action="store_true", help="Flag: user is requesting web/current info"
    )
    args = parser.parse_args()

    print(f"Fetching task context for scope: {args.scope}", file=sys.stderr)
    task_context = get_task_context(
        project_scope=args.scope,
        model_scope=args.model,
        task_hint=args.hint,
        recent_tasks=3,
    )

    result = assess_task_readiness(
        task_description=args.task,
        project_scope=args.scope,
        model_scope=args.model,
        task_context=task_context,
        user_requested_web=args.web,
        task_hint=args.hint,
    )

    result["task_context_summary"] = task_context.get("summary", {})
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ready"] else 1)


if __name__ == "__main__":
    main()
