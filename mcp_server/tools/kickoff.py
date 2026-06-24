from __future__ import annotations

import json
import re
from typing import Any

from app.llm_provider import get_llm_client
from app.db import get_store


def _normalize_slug(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "project"


def _build_kickoff_prompt(project_name: str, discussion: str) -> str:
    return f"""You are a project-memory extraction assistant. Your job is to extract durable,
actionable decisions and constraints from a project design discussion so they can
be stored as memory atoms for future reference.

Project name: {project_name}

Extract ONLY items that a developer working on this project must know and follow.
Focus on:
- Tech stack choices (frameworks, databases, build tools, languages, libraries)
- Architectural decisions (patterns, layers, integration styles)
- Hard constraints and "must not" rules
- Naming or structural conventions
- Key integrations and their roles (auth library, background job system, etc.)

Rules:
- Each item must be a complete, standalone sentence — a developer reading it cold must understand it without context.
- memory_type must be one of: decision, instruction, fact
  - decision: a chosen approach (e.g. "This project uses ESBuild for asset bundling.")
  - instruction: a rule or constraint (e.g. "Do not use React; use Hotwire instead.")
  - fact: a stable truth about the project (e.g. "Authentication is handled by Devise.")
- importance: 0.7–0.9 for tech stack / architecture; 0.8–0.95 for hard constraints.
- confidence: 0.9 if clearly stated; 0.75 if implied but not explicit.
- Do NOT extract vague statements, aspirations, or things "to consider later."
- Do NOT invent decisions not present in the discussion.
- Return an empty candidates array if the discussion contains no clear decisions.

Return ONLY strict JSON — no prose, no markdown:
{{
  "candidates": [
    {{
      "content": <string>,
      "context_summary": <string|null>,
      "memory_type": <"decision"|"instruction"|"fact">,
      "confidence": <float 0-1>,
      "importance": <float 0-1>
    }}
  ]
}}

<discussion>
{discussion.strip()}
</discussion>"""


def kickoff_project(
    project_name: str,
    discussion: str,
) -> dict[str, Any]:
    """Extract architectural decisions from a design discussion and store them
    under a named project scope.

    Uses qwen3:8b to identify tech stack choices, architectural decisions, hard
    constraints, and conventions from the discussion text.  Each extracted item
    is stored as a memory atom with scope='project:<slug>' so it surfaces
    automatically when working on that project via memory_project_context or
    memory_search with the same scope filter.

    Scope convention:
        scope = "project:<slug>"  (slug is project_name lowercased, non-alnum → '-')
        e.g. "My Rails App" → "project:my-rails-app"

    Project scope is the hard boundary for isolation.  memory_project_context
    filters by exact scope match; embeddings are semantic pointers *inside* that
    boundary.

    Args:
        project_name: Human-readable project name (e.g. 'My Rails App').
            Normalised to a URL-safe slug automatically.
        discussion: The design discussion text to process.  Paste the relevant
            portion of the planning conversation or a written design brief.
            Only clearly stated decisions are extracted — vague possibilities
            and aspirations are ignored.

    Returns:
        {
            "scope":              str,   # e.g. "project:my-rails-app"
            "stored":             list,  # atoms written this call
            "skipped_duplicates": int,   # exact-duplicate atoms skipped
            "count":              int,   # len(stored)
        }
        or {"error": str} on input/parse failure.
    """
    if not project_name or not project_name.strip():
        return {"error": "project_name is required."}
    if not discussion or not discussion.strip():
        return {"error": "discussion is required."}

    slug = _normalize_slug(project_name)
    scope = f"project:{slug}"

    prompt = _build_kickoff_prompt(project_name, discussion)
    raw = get_llm_client().generate_response(prompt)

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        return {
            "error": "LLM returned no parseable JSON.",
            "scope": scope,
            "stored": [],
            "skipped_duplicates": 0,
            "count": 0,
        }

    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        return {
            "error": f"JSON parse error: {exc}",
            "scope": scope,
            "stored": [],
            "skipped_duplicates": 0,
            "count": 0,
        }

    candidates = data.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []

    store = get_store()
    stored: list[dict[str, Any]] = []
    skipped = 0

    for c in candidates:
        if not isinstance(c, dict):
            continue
        content = str(c.get("content") or "").strip()
        if not content:
            continue

        memory_type = str(c.get("memory_type") or "decision").strip().lower()
        if memory_type not in {"decision", "instruction", "fact"}:
            memory_type = "decision"

        context_summary = str(c.get("context_summary") or "").strip() or content
        confidence = max(0.0, min(float(c.get("confidence") or 0.85), 1.0))
        importance = max(0.0, min(float(c.get("importance") or 0.8), 1.0))

        if store.find_exact_content_match(content):
            skipped += 1
            continue

        atom_id, signal_id = store.store_memory_with_signal(
            content=content,
            context_summary=context_summary,
            memory_type=memory_type,
            scope=scope,
            confidence=confidence,
            importance=importance,
            relationship="new",
            reconciliation_reason=f"project kickoff: {project_name}",
            source_key="project_kickoff",
            source_type="local",
        )
        stored.append({
            "atom_id": atom_id,
            "memory_type": memory_type,
            "content": content,
        })

    return {
        "scope": scope,
        "stored": stored,
        "skipped_duplicates": skipped,
        "count": len(stored),
    }
