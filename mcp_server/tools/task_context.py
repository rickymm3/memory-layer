from __future__ import annotations

from typing import Any

import psycopg

from app.config import get_config


def get_task_context(
    project_scope: str,
    model_scope: str | None = None,
    task_hint: str | None = None,
    recent_tasks: int = 5,
) -> dict[str, Any]:
    """Return a complete task-orientation snapshot for a single project+model pair.

    Combines three complementary views in one round-trip:
    1. **project_context** — high-importance, high-confidence atoms for the
       project scope; same content as memory_project_context but with a lower
       default threshold so more lessons surface.
    2. **model_lessons** — all active atoms from *model_scope* (weaknesses,
       prompt adaptations, reliability observations), ordered by importance.
    3. **recent_task_runs** — the last N task_run records for this project,
       newest first; shows what tasks were completed and their outcomes.

    If *task_hint* is given, a fourth section is returned:
    4. **task_relevant_atoms** — atoms from project_scope that are semantically
       related to the task hint. Requires an embedding call (adds ~500 ms).

    Call this as the first tool when starting any non-trivial task. One call
    replaces: memory_project_context + memory_recent (model scope) +
    memory_list_task_runs.

    Args:
        project_scope: Required. Project scope (e.g. 'project:memory-layer').
        model_scope: Optional model scope (e.g. 'model:qwen3-8b'). When
            provided, all active atoms from this scope are returned as
            model_lessons so the agent sees known weaknesses and prompt
            adaptations before planning.
        task_hint: Optional short description of the task about to be
            performed. When provided, triggers a semantic search over
            project_scope atoms and returns the top 5 most relevant results
            as task_relevant_atoms.
        recent_tasks: Number of recent task_run records to return. Clamped
            to 1–20. Default 5.
    """
    clamped_tasks = max(1, min(int(recent_tasks), 20))
    config = get_config()

    project_atoms: list[dict[str, Any]] = []
    model_lessons: list[dict[str, Any]] = []
    task_runs: list[dict[str, Any]] = []

    with psycopg.connect(config.database_url) as conn:
        # --- 1. project_context -------------------------------------------------
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, context_summary, memory_type, scope,
                       confidence, importance, created_at, lifecycle_status,
                       disagreement_flag
                FROM memory_atoms
                WHERE scope = %s
                  AND importance >= 0.5
                  AND confidence >= 0.6
                  AND lifecycle_status = 'active'
                ORDER BY importance DESC, confidence DESC, created_at DESC
                LIMIT 15;
                """,
                (project_scope,),
            )
            for r in cur.fetchall():
                project_atoms.append(
                    {
                        "id": str(r[0]),
                        "content": r[1],
                        "context_summary": r[2],
                        "memory_type": r[3],
                        "scope": r[4],
                        "confidence": round(float(r[5]), 3),
                        "importance": round(float(r[6]), 3),
                        "created_at": r[7].isoformat() if r[7] else None,
                        "lifecycle_status": r[8],
                        "disagreement_flag": bool(r[9]),
                    }
                )

        # --- 2. model_lessons ---------------------------------------------------
        if model_scope is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, content, context_summary, memory_type, scope,
                           confidence, importance, created_at, disagreement_flag
                    FROM memory_atoms
                    WHERE scope = %s
                      AND lifecycle_status = 'active'
                    ORDER BY importance DESC, confidence DESC, created_at DESC
                    LIMIT 20;
                    """,
                    (model_scope,),
                )
                for r in cur.fetchall():
                    model_lessons.append(
                        {
                            "id": str(r[0]),
                            "content": r[1],
                            "context_summary": r[2],
                            "memory_type": r[3],
                            "scope": r[4],
                            "confidence": round(float(r[5]), 3),
                            "importance": round(float(r[6]), 3),
                            "created_at": r[7].isoformat() if r[7] else None,
                            "disagreement_flag": bool(r[8]),
                        }
                    )

        # --- 3. recent_task_runs ------------------------------------------------
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, scope, task_description, model_used,
                       outcome, lessons_stored, created_at
                FROM task_runs
                WHERE scope = %s
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (project_scope, clamped_tasks),
            )
            for r in cur.fetchall():
                task_runs.append(
                    {
                        "id": str(r[0]),
                        "scope": r[1],
                        "task_description": r[2],
                        "model_used": r[3],
                        "outcome": r[4],
                        "lessons_stored": r[5],
                        "created_at": r[6].isoformat() if r[6] else None,
                    }
                )

    # --- 4. task_relevant_atoms (semantic search) --------------------------------
    task_relevant: list[dict[str, Any]] = []
    if task_hint is not None:
        from app.llm_provider import get_llm_client

        embedding = get_llm_client().embed_text(task_hint)
        embedding_literal = "[" + ",".join(str(v) for v in embedding) + "]"

        with psycopg.connect(config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, content, context_summary, memory_type, scope,
                           confidence, importance,
                           1 - (embedding <=> %s::vector) AS similarity,
                           disagreement_flag
                    FROM memory_atoms
                    WHERE scope = %s
                      AND lifecycle_status = 'active'
                      AND 1 - (embedding <=> %s::vector) >= 0.45
                    ORDER BY embedding <=> %s::vector
                    LIMIT 5;
                    """,
                    (embedding_literal, project_scope,
                     embedding_literal, embedding_literal),
                )
                for r in cur.fetchall():
                    task_relevant.append(
                        {
                            "id": str(r[0]),
                            "content": r[1],
                            "context_summary": r[2],
                            "memory_type": r[3],
                            "scope": r[4],
                            "confidence": round(float(r[5]), 3),
                            "importance": round(float(r[6]), 3),
                            "similarity": round(float(r[7]), 3),
                            "disagreement_flag": bool(r[8]),
                        }
                    )

    outcome_counts: dict[str, int] = {}
    for tr in task_runs:
        outcome_counts[tr["outcome"]] = outcome_counts.get(tr["outcome"], 0) + 1

    return {
        "project_scope": project_scope,
        "model_scope": model_scope,
        "project_context": project_atoms,
        "model_lessons": model_lessons,
        "recent_task_runs": task_runs,
        "task_relevant_atoms": task_relevant,
        "summary": {
            "project_atoms": len(project_atoms),
            "model_lessons": len(model_lessons),
            "recent_task_runs": len(task_runs),
            "task_relevant_atoms": len(task_relevant),
            "task_run_outcomes": outcome_counts,
        },
    }
