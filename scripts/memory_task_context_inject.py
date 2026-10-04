#!/usr/bin/env python3
"""UserPromptSubmit / SessionStart hook — inject memory_task_context on session start.

Fires when no session marker exists (first turn, or post-compaction after marker deletion).
Calls /mcp/sse with tool=memory_task_context and injects the result as additionalContext.
Falls back to a text enforcement block if the site is unreachable — never blocks the prompt.

Replaces memory-check.sh.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


def _load_env(project_dir: str) -> None:
    env_path = Path(project_dir) / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _atom_text(atom, prefer_summary: bool = True) -> str:
    """memory_task_context returns compact strings by default, dicts otherwise."""
    if isinstance(atom, str):
        return atom
    if not isinstance(atom, dict):
        return ""
    text = (atom.get("context_summary") if prefer_summary else None) or atom.get("content") or ""
    mtype = atom.get("memory_type", "")
    return f"[{mtype}] {text}" if text else ""


def _format_context(result: dict) -> str:
    lines = ["## MEMORY — SESSION CONTEXT (auto-loaded)"]

    for title, key, cap in (
        ("Project Context", "project_context", 6),
        ("User Preferences", "user_context", 5),
        ("Model Directives", "model_lessons", 5),
    ):
        items = [t for t in (_atom_text(a) for a in (result.get(key) or [])[:cap]) if t]
        if items:
            lines.append(f"\n### {title}")
            lines.extend(items)

    write_protocol = result.get("write_protocol") or ""
    if isinstance(write_protocol, dict):
        write_protocol = write_protocol.get("mandate") or write_protocol.get("rule") or ""
    if write_protocol:
        lines.append(f"\n### Write Rule\n{write_protocol}")
    else:
        lines.append(
            "\n### Write Rule\n"
            "Call memory_store_auto BEFORE finishing any turn where the user "
            "expresses a preference, correction, decision, or instruction. "
            "Scope: project facts → project:memory-layer, "
            "model observations → model:claude-sonnet-4-6, "
            "user preferences → user."
        )

    return "\n".join(lines)


def _emit(context: str, event: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }))


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--event", default="UserPromptSubmit")
    args, _ = parser.parse_known_args()
    event = args.event

    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        sys.exit(0)

    session_id = (data.get("session_id") or "").strip()
    prompt = (data.get("prompt") or "")[:200]

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    _load_env(project_dir)

    marker_dir = Path.home() / ".claude" / "memory-sessions"
    marker = marker_dir / f"{session_id}.initialized" if session_id else None

    # Already initialized this session — stay silent
    if marker and marker.exists():
        sys.exit(0)

    base_url = (os.environ.get("MEMORY_LAYER_URL") or "").rstrip("/")
    token = os.environ.get("MEMORY_LAYER_TOKEN") or ""

    if not base_url or not token:
        # Site config missing — inject text enforcement fallback
        _emit(
            "=== MEMORY LAYER — SESSION START ===\n"
            "Call memory_task_context(project_scope='project:memory-layer', "
            "model_scope='model:claude-sonnet-4-6', "
            "task_hint='<one sentence describing what user is asking>') "
            "before responding. Do not answer until context is loaded.\n"
            "=== END ===",
            event,
        )
        return

    body = json.dumps({
        "tool": "memory_task_context",
        "args": {
            "project_scope": "project:memory-layer",
            "model_scope": "model:claude-sonnet-4-6",
            "task_hint": prompt or "session start — loading project context",
            "compact": True,
        },
    }).encode()

    try:
        req = urllib.request.Request(
            base_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
    except Exception:
        # Site unreachable — inject text enforcement fallback
        _emit(
            "=== MEMORY LAYER — SESSION START (site unreachable) ===\n"
            "Call memory_task_context(project_scope='project:memory-layer', "
            "model_scope='model:claude-sonnet-4-6', "
            "task_hint='<one sentence describing what user is asking>') "
            "before responding.\n"
            "=== END ===",
            event,
        )
        return

    # Mark session as initialized
    if marker:
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text("hook-injected\n")

    # Inject rich context from task_context response
    context = _format_context(result.get("result") or result)
    if context:
        _emit(context, event)


if __name__ == "__main__":
    main()
